#!/usr/bin/env python3
"""Isolated native startup/module/AMI checks. Never originate or register a call."""

import argparse
import json
import re
import subprocess
import time
import uuid


class Smoke:
    def __init__(self, context, image):
        if context != "colima-faxbot-refresh":
            raise ValueError("This smoke is restricted to the private Faxbot context")
        self.docker = ["docker", "--context", context]
        self.image = image
        self.prefix = "faxbot-native-smoke-" + uuid.uuid4().hex[:12]
        self.containers = []
        self.network_created = False

    def command(self, *args, check=True, input_text=None):
        result = subprocess.run(
            self.docker + list(args),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if check and result.returncode:
            raise RuntimeError("Owned Docker smoke command failed")
        return result

    def start(self, name, environment):
        container = self.prefix + "-" + name
        args = [
            "run",
            "--detach",
            "--name",
            container,
            "--network",
            self.prefix,
            "--label",
            "com.faxbot.scope=native-smoke",
        ]
        for key, value in environment.items():
            args.extend(["--env", key + "=" + value])
        self.command(*args, self.image)
        self.containers.append(container)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            result = self.command(
                "exec", container, "asterisk", "-rx", "core show version", check=False
            )
            if result.returncode == 0 and "Asterisk 22.11.0" in result.stdout:
                return container
            state = self.command(
                "inspect", "--format", "{{.State.Running}}", container
            ).stdout.strip()
            if state != "true":
                raise RuntimeError("Owned Asterisk container exited during startup")
            time.sleep(0.25)
        raise RuntimeError("Owned Asterisk startup timed out")

    def cli(self, container, query, expected):
        output = self.command("exec", container, "asterisk", "-rx", query).stdout
        if expected not in output:
            raise AssertionError("Native CLI capability check failed: " + query)
        return output

    def protocol(self, container, username, password):
        # Docker internal networks suppress published ports. Probe the real TCP
        # listener on container loopback without giving the network egress.
        ports = self.command(
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", container
        ).stdout
        assert json.loads(ports) in (None, {}), "Smoke must not expose host ports"

        def exchange(actions):
            output = self.command(
                "exec",
                "--interactive",
                container,
                "timeout",
                "10",
                "nc",
                "-N",
                "-w",
                "5",
                "127.0.0.1",
                "5038",
                input_text=actions,
            ).stdout
            responses = {}
            for block in re.split(r"\r?\n\r?\n", output):
                fields = {}
                for line in block.splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key] = value.strip()
                if "Response" in fields:
                    responses[fields.get("ActionID")] = fields
            return responses

        replies = exchange(
            "Action: Login\r\nActionID: startup-smoke\r\nUsername: "
            + username
            + "\r\nSecret: "
            + password
            + "\r\nEvents: off\r\n\r\n"
            "Action: Ping\r\nActionID: ping-smoke\r\n\r\n"
            "Action: Command\r\nActionID: forbidden-smoke\r\n"
            "Command: core show version\r\n\r\n"
        )
        assert replies.get("startup-smoke", {}).get("Response") == "Success"
        assert replies.get("ping-smoke", {}).get("Response") == "Success"
        assert (
            replies.get("forbidden-smoke", {}).get("Response") == "Error"
        ), "AMI user unexpectedly has remote command authority"
        replies = exchange(
            "Action: Login\r\nActionID: denied-smoke\r\nUsername: "
            + username
            + "\r\nSecret: wrong-smoke-secret\r\n\r\n"
        )
        assert replies.get("denied-smoke", {}).get("Response") == "Error"

    def invalid_configuration(self, environment, expected):
        args = ["run", "--rm", "--network", "none"]
        for key, value in environment.items():
            args.extend(["--env", key + "=" + value])
        result = self.command(*args, self.image, check=False)
        assert result.returncode != 0 and expected in result.stderr
        for value in environment.values():
            assert (
                value not in result.stderr + result.stdout
            ), "Startup exposed supplied configuration"

    def run(self):
        identity = json.loads(self.command("image", "inspect", self.image).stdout)[0]
        self.command(
            "network",
            "create",
            "--internal",
            "--label",
            "com.faxbot.scope=native-smoke",
            self.prefix,
        )
        self.network_created = True
        offline = self.start("offline", {})
        self.cli(offline, "core show function BASE64_DECODE", "BASE64_DECODE")
        self.cli(offline, "core show application SendFAX", "SendFAX")
        self.cli(offline, "fax show capabilities", "Spandsp")
        self.cli(offline, "module show like chan_pjsip", "chan_pjsip.so")
        self.cli(offline, "dialplan show faxbot-send", "SendFAX")
        settings = self.cli(offline, "manager show settings", "Manager")
        assert re.search(r"Manager \(AMI\):\s*No\b", settings)
        registrations = self.command(
            "exec", offline, "asterisk", "-rx", "pjsip show registrations"
        ).stdout
        assert "No objects found" in registrations
        username, password = "native_smoke", 'Synthetic-$#"=é-' + uuid.uuid4().hex
        environment = {
            "ASTERISK_AMI_USERNAME": username,
            "ASTERISK_AMI_PASSWORD": password,
            "SIP_USERNAME": "synthetic-smoke",
            "SIP_PASSWORD": "Synthetic-" + uuid.uuid4().hex,
            "SIP_SERVER": "127.0.0.1:5099",
            "SIP_FROM_DOMAIN": "localhost",
            "SIP_REGISTER": "false",
        }
        configured = self.start("configured", environment)
        self.cli(configured, "pjsip show endpoint trunk-endpoint", "trunk-endpoint")
        self.cli(configured, "pjsip show aor trunk-aor", "sip:127.0.0.1:5099")
        registrations = self.command(
            "exec", configured, "asterisk", "-rx", "pjsip show registrations"
        ).stdout
        assert "No objects found" in registrations
        self.protocol(configured, username, password)
        self.invalid_configuration(
            {"ASTERISK_AMI_USERNAME": "partial-smoke"}, "Incomplete AMI configuration"
        )
        self.invalid_configuration(
            {"SIP_USERNAME": "partial-smoke"}, "Incomplete SIP configuration"
        )
        self.invalid_configuration(
            {
                "ASTERISK_AMI_USERNAME": username,
                "ASTERISK_AMI_PASSWORD": "private;injection",
            },
            "Unsupported AMI configuration syntax",
        )
        self.invalid_configuration(
            {
                "ASTERISK_AMI_USERNAME": "GENERAL",
                "ASTERISK_AMI_PASSWORD": "synthetic-reserved-secret",
            },
            "Unsupported AMI configuration syntax",
        )
        self.invalid_configuration(
            {
                "SIP_USERNAME": "synthetic-smoke",
                "SIP_PASSWORD": "private;injection",
                "SIP_SERVER": "127.0.0.1:5099",
            },
            "Unsupported SIP configuration syntax",
        )
        print(
            json.dumps(
                {
                    "image_id": identity["Id"],
                    "architecture": identity["Architecture"],
                    "asterisk": "22.11.0",
                    "checks": [
                        "offline startup",
                        "required modules",
                        "dedicated dialplan",
                        "loopback trunk projection",
                        "AMI login/ping/rejection",
                        "remote command refused",
                        "unsafe/partial config refusal",
                    ],
                },
                indent=2,
            )
        )

    def close(self):
        for container in reversed(self.containers):
            self.command("rm", "--force", "--volumes", container, check=False)
        if self.network_created:
            self.command("network", "rm", self.prefix, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="colima-faxbot-refresh")
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    smoke = Smoke(args.context, args.image)
    try:
        smoke.run()
    finally:
        smoke.close()


if __name__ == "__main__":
    main()
