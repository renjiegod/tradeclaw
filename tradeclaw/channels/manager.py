from __future__ import annotations


class ChannelManager:
    def __init__(self, service, approval_gate):
        self.service = service
        self.approval_gate = approval_gate

    def handle_command(self, command_text: str) -> str:
        parts = command_text.strip().split()
        if not parts:
            return "empty command"

        command = parts[0].lower()
        args = parts[1:]

        if command == "/start":
            if not args:
                return "usage: /start <instance_id_or_name>"
            instance = self.service.start_instance(args[0])
            return f"instance {instance.instance_id} running"

        if command == "/stop":
            if not args:
                return "usage: /stop <instance_id_or_name>"
            instance = self.service.stop_instance(args[0])
            return f"instance {instance.instance_id} stopped"

        if command == "/status":
            if not args:
                return "usage: /status <instance_id_or_name>"
            status = self.service.get_instance_status(args[0])
            return f"instance {status['instance_id']} status={status['status']} cycles={status['cycles']}"

        if command == "/approve":
            if not args:
                return "usage: /approve <approval_id>"
            result = self.approval_gate.approve(args[0])
            return f"approval {result.approval_id} approved for {result.intent_id}"

        if command == "/reject":
            if not args:
                return "usage: /reject <approval_id>"
            result = self.approval_gate.reject(args[0], reason="manual reject")
            return f"approval {result.approval_id} rejected for {result.intent_id}"

        if command == "/kill":
            enabled = True
            if args and args[0].lower() in {"off", "disable", "0", "false"}:
                enabled = False
            self.service.set_kill_switch(enabled)
            return f"kill switch {'enabled' if enabled else 'disabled'}"

        return f"unknown command: {command}"
