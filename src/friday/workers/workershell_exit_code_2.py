"""
Shell command execution worker for Friday.

Executes arbitrary shell commands and captures output. Handles both
successful and failed command exits gracefully, returning the exit code
and stderr/stdout in the output field.
"""

import json
import subprocess
from pathlib import Path

WORKER_NAME = "shell"
WORKER_CAPABILITIES = ["Shell Commands", "Infrastructure", "File Editing", "Research"]
WORKER_EXAMPLE_INPUT = json.dumps("ls -la")


def execute(input_data: str, workspace: str = ".") -> dict:
    """
    Execute a shell command and return its output.
    
    Args:
        input_data: JSON string containing the shell command to execute
        workspace: Directory to execute the command in
    
    Returns:
        dict with success, output, and optional error fields
    """
    try:
        command = json.loads(input_data)
        
        if not isinstance(command, str):
            return {
                "success": False,
                "error": f"Expected string command, got {type(command).__name__}"
            }
        
        workspace_path = Path(workspace)
        if not workspace_path.exists():
            workspace_path.mkdir(parents=True, exist_ok=True)
        
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output_lines = []
        if result.stdout:
            output_lines.append(result.stdout)
        if result.stderr:
            output_lines.append(result.stderr)
        
        output_text = "".join(output_lines) if output_lines else ""
        
        return {
            "success": True,
            "output": output_text if output_text else f"Command exited with code {result.returncode}"
        }
    
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Command execution timed out after 30 seconds"
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
