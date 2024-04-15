""" PyNuclei Module - allow running nuclei from Python """
import json
import random
import subprocess
import os
import shutil
import tempfile

import queue
import threading

import time
import datetime
import requests
import yaml

from .ScanUtils.UserAgents import USER_AGENTS

FILE_SEPARATOR = "#SEP#"
DEBUG = False


class NucleiNotFound(Exception):
    """
    Exception for not finding Nuclei in the system
    """


class Nuclei:
    """
    Class handling the Nuclei scans and result generation.
    """

    def __init__(self, nuclei_path=None):
        self.running = 0
        self.done = 0
        self.eta = datetime.timedelta(seconds=0)
        self.max_progress = 0
        self.current_progress = 0
        self.verbose = False
        self.selected_templates_count = 0
        self.processes = []
        self.findings = 0
        self.max_threads = 3  # maximum number of instances to run at the same time

        self.active_metric_ports = {}

        self.queue = queue.Queue()

        # Allow changing the path where nuclei is installed (instead of expecting it to be in $PATH)
        # Check if the '/' is at the end - and remove it if "yes"
        if nuclei_path is not None and nuclei_path[-1] == "/":
            nuclei_path = nuclei_path[:-1]
        self.nuclei_path = nuclei_path

        Nuclei.check_first_run(nuclei_path)
        self.output_path = f"{tempfile.gettempdir()}/"
        try:
            os.makedirs(os.path.expanduser(self.output_path))
        except FileExistsError:
            pass

    def metrics_thread(self, max_metrics_ports):
        """Connect to the /metrics backend and make stats from it"""

        # Wait until we see at least one running before exiting
        # It takes a few milliseconds for things to start, don't stop immediately
        # As it will appear at the beginning that no process is running
        wait_for_running = True
        progress_values = {}

        while True:
            self.running = 0

            ports = list(self.active_metric_ports.keys())
            for port in ports:
                if not self.active_metric_ports[port]:
                    continue

                if DEBUG:
                    print(f"Monitoring metrics port: {port}")

                if port not in progress_values:
                    progress_values[port] = {}
                    progress_values[port]["done"] = False
                    progress_values[port]["start_time"] = datetime.datetime.now()
                    progress_values[port]["max"] = 1
                    progress_values[port]["current"] = 0
                    progress_values[port]["matched"] = 0
                    progress_values[port]["eta"] = datetime.timedelta(seconds=0)
                    progress_values[port]["retry_count"] = 0

                try:
                    response = requests.get(
                        f"http://127.0.0.1:{port}/metrics", timeout=1
                    )
                except requests.ConnectionError as _:
                    if progress_values[port]["retry_count"] < 5:
                        progress_values[port]["retry_count"] += 1
                        if DEBUG:
                            print(
                                f"{port} found to be closed, will retry "
                                f"#{progress_values[port]['retry_count']}"
                            )
                        continue

                    if port in progress_values and "max" in progress_values[port]:
                        self.active_metric_ports[port] = False
                        progress_values[port]["done"] = True
                        progress_values[port]["current"] = progress_values[port]["max"]

                    continue

                json_object = {}

                # If the port is closed, then the process is done
                # If there is a malformed JSON, we don't really know
                try:
                    json_object = response.json()
                    self.running += 1
                    wait_for_running = False
                except Exception as _:
                    self.active_metric_ports[port] = False
                    continue

                progress_values[port]["matched"] = json_object["matched"]
                progress_values[port]["done"] = False
                progress_values[port]["max"] = json_object["total"]
                progress_values[port]["current"] = json_object["requests"]

            # Since we moved to a queue, we need to make the max_progress reflect
            #  those nuclei that didn't start, we will assume each has 10 tests to
            #  run so that we don't reach 100% before all the nuclei are done
            # How many nuclei we sill need to run
            nuclei_left = (max_metrics_ports - 9092) - len(progress_values)
            self.max_progress = nuclei_left * 10

            self.current_progress = 0
            self.eta = datetime.timedelta(seconds=0)
            self.findings = 0

            for _, item in progress_values.items():
                self.max_progress += item["max"]
                self.current_progress += item["current"]
                self.findings += item["matched"]
                if item["eta"] > self.eta:
                    self.eta = item["eta"]

                if item["current"] > 0 and item["max"] > 0:
                    if not item["done"]:
                        progress = item["current"] / item["max"] * 100.0
                        if progress != 100:
                            current_time = datetime.datetime.now()
                            item["eta"] = (
                                (current_time - item["start_time"])
                                / progress
                                * (100 - progress)
                            )
                        else:
                            item["eta"] = datetime.timedelta(seconds=0)
                    else:
                        item["eta"] = datetime.timedelta(seconds=0)

            done_count = 0
            for port in self.active_metric_ports:
                if not self.active_metric_ports[port]:
                    done_count += 1

            self.done = done_count

            if (
                self.running == 0
                and done_count == (max_metrics_ports - 9092)
                and not wait_for_running
            ):
                # No more running processes
                break

            if DEBUG:
                print(f"{self.max_progress=} {self.current_progress=} {self.findings=}")

            time.sleep(1)  # Sleep for 1sec

        if DEBUG:
            print("metrics_thread is done")

    def stop(self):
        """Allow stopping of nuclei processes"""
        for process in self.processes:
            process.send_signal(2)  # SIGINT

    def scanning_thread(self):
        """Launch the nuclei process and output the outcome if 'verbose'"""
        while True:
            try:
                (host, metrics_port, command, verbose) = self.queue.get(timeout=1)
            except queue.Empty:
                if DEBUG:
                    print("No more scan commands in queue")

                return

            self.active_metric_ports[metrics_port] = True
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self.processes.append(process)

            output, error = process.communicate()

            if verbose:
                print(f"[Stdout] [{host}] {output.decode('utf-8', 'ignore')}")
                print(f"[Stderr] [{host}] {error.decode('utf-8', 'ignore')}")

            self.queue.task_done()

    @staticmethod
    def is_nuclei_installed(nuclei_path=None):
        """
        Checks whether Nuclei is installed
            - Use nuclei_path, to override the path
        """
        is_installed = shutil.which("nuclei", path=nuclei_path)
        if is_installed is None:
            raise NucleiNotFound("Nuclei not found in path")

    @staticmethod
    def check_first_run(nuclei_path=None):
        """
        Checks if the PyNuclei module was run for the first time - if yes, update the templates
        """
        nuclei_config_filename = f"{os.path.dirname(__file__)}/.config"
        with open(nuclei_config_filename, "r+", encoding="latin1") as py_nuclei_config:
            config_details = {}
            try:
                config_details = json.loads(py_nuclei_config.read())
            except Exception:
                print(f"Malformed nuclei config file: {nuclei_config_filename}")

            run_update = False
            if (
                "FIRST_RUN" not in config_details
                or config_details["FIRST_RUN"]
                or "LAST_RUN" not in config_details
            ):
                print("Running nuclei update")
                run_update = True

            if "LAST_RUN" in config_details:
                try:
                    elapsed: datetime.timedelta = (
                        datetime.datetime.now()
                        - datetime.datetime.strptime(
                            config_details["LAST_RUN"], "%y-%m-%d"
                        )
                    )
                    if elapsed.days > 7:
                        print(f"Running nuclei update as {elapsed} has passed")
                        run_update = True
                except Exception:
                    print("Running nuclei update")
                    run_update = True

            if run_update:
                Nuclei.update_nuclei(nuclei_path=nuclei_path)

                config_details["FIRST_RUN"] = False
                # Keep record of when we last run, try to update once a week
                config_details["LAST_RUN"] = datetime.datetime.strftime(
                    datetime.datetime.now(), "%y-%m-%d"
                )

                py_nuclei_config.seek(0)
                py_nuclei_config.truncate()
                py_nuclei_config.write(json.dumps(config_details))

    @staticmethod
    def update_nuclei(verbose=False, nuclei_path=None):
        """
        Checks and updates Nuclei.

        Checks for any updates to Nuclei or Nuclei Templates,
        and installs them if any.
        """

        # Make sure Nuclei is installed
        Nuclei.is_nuclei_installed(nuclei_path)

        processes = []

        nuclei_binary = "nuclei"
        if nuclei_path:
            nuclei_binary = f"{nuclei_path}/nuclei"
        commands = [[nuclei_binary, "-update-templates"], [nuclei_binary, "-update"]]

        for command in commands:
            processes.append(
                subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
            )

        for process in processes:
            output, error = process.communicate()
            if verbose:
                print(f"[Stdout] {output.decode('utf-8', 'ignore')}")
                print(f"[Stderr] {error.decode('utf-8', 'ignore')}")

    @property
    def ignored_templates(self):
        """Ignore slow and helper templates"""
        return [
            "headless",
            "fuzzing",
            "helpers",
        ]

    @property
    def nuclei_templates(self):
        """Return a list of usable templates"""
        return [
            "cnvd",
            "cves",
            "default-logins",
            "exposed-panels",
            "exposures",
            "file",
            "misconfiguration",
            "miscellaneous",
            "takeovers",
            "technologies",
            "token-spray",
            "vulnerabilities",
            "network",
            "dns",
            "iot",
            "ssl",
        ]

    def create_result_dir(self, host):
        """Create the result directory nuclei will use"""
        try:
            os.makedirs(os.path.expanduser(f"{self.output_path}{host}"))
        except FileExistsError:
            pass

    def return_templates_details(self):
        """
        Process the templates available and return them as a structure
        WARNING: This is a VERY time consuming function
        """
        nuclei_binary = "nuclei"
        if self.nuclei_path:
            nuclei_binary = f"{self.nuclei_path}/nuclei"

        # Get path of templates
        command = [
            nuclei_binary,
            "-disable-update-check",
            "-no-color",
            "-templates-version",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        # The output is inside error
        _, error = process.communicate()
        error = error.decode()

        if "nuclei-templates version" not in error:
            raise ValueError("Nuclei didn't return the expected output")

        if error.find("(") == -1 or error.find(")") == -1:
            raise ValueError("Nuclei didn't return the expected output")

        templates_path = error[error.find("(") + 1 : error.find(")")]
        print(f"{templates_path=}")

        # Get a list of templates
        command = [nuclei_binary, "-disable-update-check", "-no-color", "-tl"]

        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )

        output, error = process.communicate()
        output = output.decode()

        templates = []

        # Remove \r in case its there
        output = output.replace("\r", "")
        lines = output.split("\n")
        count = 0

        for line in lines:
            if line == "":
                continue

            count += 1
            if count % 100 == 0:
                print(f"Progress: {count / len(lines) * 100.0:.2f}%")

            template_filename = f"{templates_path}/{line}"

            # print(f"Opening {template_filename}")
            with open(template_filename, "r", encoding="utf-8") as file_handle:
                template_obj = yaml.safe_load(file_handle)

                keys = list(template_obj.keys())
                for key in keys:
                    # Keep only the info we want
                    if key not in ["Template", "id", "info"]:
                        del template_obj[key]

                templates.append(template_obj)

        return templates

    def _parse_nuclei_scan(self, host, templates):
        report = []

        for template in templates:
            try:
                with open(
                    f"{self.output_path}{host}{template}", "r", encoding="latin1"
                ) as scan_result:
                    report.extend(json.load(scan_result))
            except Exception as e:
                print(f"Exception while reading Nuclei Scan Result: {e}")

        return report

    def _format_nuclei_report(self, report):
        """
        Reformats the raw Nuclei scan results from file into a cleaner list.
        Args:
                report (list): The raw report from file
        Returns:
                list: The list of formatted report
        """
        formatted_report = []
        for vuln in report:
            try:
                data = {
                    "templateId": vuln["template-id"],
                    "host": vuln["host"],
                    "vulnerabilityName": vuln["info"]["name"],
                    "vulnerabilityDetail": str(),
                    "description": str(),
                    "type": vuln["type"],
                    "result": [],
                    "vulnerableAt": vuln["matched-at"],
                    "solution": str(),
                    "curl": str(),
                    "severity": vuln["info"]["severity"],
                    "tags": vuln["info"]["tags"],
                    "reference": str(),
                    "cvss-metrics": str(),
                    "cvss-score": None,
                    "cve-id": str(),
                    "cwe-id": None,
                }
                if "description" in vuln["info"]:
                    data["description"] = vuln["info"]["description"]

                if "severity" in vuln["info"]:
                    data["severity"] = vuln["info"]["severity"]

                if "reference" in vuln["info"]:
                    if vuln["info"]["reference"]:
                        if isinstance(vuln["info"]["reference"], str):
                            data["reference"] = vuln["info"]["reference"]
                        elif isinstance(vuln["info"]["reference"], list):
                            data["reference"] = ", ".join(vuln["info"]["reference"])

                if "remediation" in vuln["info"]:
                    data["solution"] = vuln["info"]["remediation"]

                if "classification" in vuln["info"]:
                    if "cvss-metrics" in vuln["info"]["classification"]:
                        data["cvss-metrics"] = vuln["info"]["classification"][
                            "cvss-metrics"
                        ]

                    if "cvss-score" in vuln["info"]["classification"]:
                        data["cvss-score"] = vuln["info"]["classification"][
                            "cvss-score"
                        ]

                    if "cve-id" in vuln["info"]["classification"]:
                        data["cve-id"] = vuln["info"]["classification"]["cve-id"]

                    if "cwe-id" in vuln["info"]["classification"]:
                        cwe = 0
                        if (
                            isinstance(vuln["info"]["classification"]["cwe-id"], list)
                            and vuln["info"]["classification"]["cwe-id"]
                        ):
                            cwe = vuln["info"]["classification"]["cwe-id"][0]
                        else:
                            cwe = vuln["info"]["classification"]["cwe-id"]

                        if cwe is not None:
                            if "cwe-" in cwe.lower():
                                data["cwe-id"] = int(cwe.split("-")[-1])

                if "extracted-results" in vuln:
                    data["result"] = vuln["extracted-results"]

                if "curl-command" in vuln:
                    data["curl"] = vuln["curl-command"]

                if "matcher-name" in vuln:
                    data["vulnerabilityDetail"] = vuln["matcher-name"]

                formatted_report.append(data)
            except Exception as e:
                print(f"Error in parsing Nuclei result: {e} | Data: {vuln}")
                continue

        return formatted_report

    def scan(
        self,
        host,
        templates=[],
        user_agent="",
        rate_limit=150,
        verbose=False,
        metrics=False,
        max_host_error=30,
    ):
        """
        Runs the nuclei scan and returns a formatted dictionary with the results.
        Args:
                host [str]: The hostname of the target which Nuclei will run against
                templates [list][Optional]: If templates list not provided all nuclei templates from
                                            "nuclei_templates" property will be executed
                user_agents [str][Optional]: If not provided random User-Agents will be used.
                rate_limit [int][Optional]: Defaults to 150.
        Returns:
                result [dict]: Scan result from all templates.
        """
        self.verbose = verbose
        Nuclei.is_nuclei_installed(self.nuclei_path)

        file_name_valid_host = f"{host.replace('/', FILE_SEPARATOR)}/"
        self.create_result_dir(file_name_valid_host)

        if not templates:
            templates = self.nuclei_templates

        self.selected_templates_count = len(templates)
        metrics_port = 9092

        threads = []
        for _ in range(self.max_threads):
            t = threading.Thread(target=self.scanning_thread)
            threads.append(t)
            t.start()

        for template in templates:
            if not user_agent:
                user_agent = random.choice(USER_AGENTS)

            nuclei_binary = "nuclei"
            if self.nuclei_path:
                nuclei_binary = f"{self.nuclei_path}/nuclei"

            command = [
                nuclei_binary,
                "-header",
                f"'User-Agent: {user_agent}'",
                "-rate-limit",
                str(rate_limit),
                "-target",
                host,
                "-templates",
                f"{template}/",
                "--json-export",
                f"{self.output_path}{file_name_valid_host}{template}",
                "-disable-update-check",
            ]

            if max_host_error != 30:
                command.append("-max-host-error")
                command.append(str(max_host_error))

            if metrics:
                command.append("-stats")
                command.append("-metrics-port")
                command.append(str(metrics_port))
                command.append("-stats-interval")
                command.append("1")  # Update very 1 second
                metrics_port += 1

            # commands.append(command)
            self.queue.put([host, metrics_port, command, self.verbose])

        if metrics:
            t = threading.Thread(target=self.metrics_thread, args=[metrics_port])
            threads.append(t)
            t.start()

        self.queue.join()

        for thread in threads:
            thread.join()

        report = self._parse_nuclei_scan(file_name_valid_host, templates)

        shutil.rmtree(f"{self.output_path}{file_name_valid_host}", ignore_errors=True)

        return self._format_nuclei_report(report)
