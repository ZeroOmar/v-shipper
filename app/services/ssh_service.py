"""SSH service for remote operations."""

import base64
import paramiko
from pathlib import Path
from typing import Optional
import io


class SSHService:
    """Service for SSH operations on remote hosts."""
    
    SSH_PORT = 22
    TIMEOUT = 30
    
    def __init__(self):
        self.connections = {}  # Cache of SSH connections
    
    def connect(self, host: str, username: str, ssh_key_b64: str) -> paramiko.SSHClient:
        """Establish SSH connection to remote host."""
        connection_key = f"{host}:{username}"
        
        try:
            # Check if connection already exists and is alive
            if connection_key in self.connections:
                client = self.connections[connection_key]
                try:
                    client.get_transport().is_active()
                    return client
                except Exception:
                    pass  # Connection dead, create new one
            
            # Decode SSH key from base64
            ssh_key_bytes = base64.b64decode(ssh_key_b64)
            ssh_key_file = io.StringIO(ssh_key_bytes.decode('utf-8'))
            
            # Create SSH client
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            # Load SSH key
            pkey = paramiko.RSAKey.from_private_key(ssh_key_file)
            
            # Connect
            client.connect(
                hostname=host,
                port=self.SSH_PORT,
                username=username,
                pkey=pkey,
                timeout=self.TIMEOUT,
                look_for_keys=False,
                allow_agent=False
            )
            
            # Cache connection
            self.connections[connection_key] = client
            print(f"[SSH] Connected to {host}", flush=True)
            
            return client
        
        except Exception as e:
            print(f"[ERROR] SSH connection failed to {host}: {e}", flush=True)
            raise
    
    def execute_command(self, client: paramiko.SSHClient, command: str) -> tuple:
        """Execute command on remote host."""
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=self.TIMEOUT)
            output = stdout.read().decode('utf-8')
            error = stderr.read().decode('utf-8')
            exit_code = stdout.channel.recv_exit_status()
            
            return exit_code, output, error
        except Exception as e:
            print(f"[ERROR] SSH command execution failed: {e}", flush=True)
            raise
    
    def close(self, host: str, username: str):
        """Close SSH connection."""
        connection_key = f"{host}:{username}"
        if connection_key in self.connections:
            try:
                self.connections[connection_key].close()
                del self.connections[connection_key]
                print(f"[SSH] Disconnected from {host}", flush=True)
            except Exception as e:
                print(f"[WARNING] Failed to close SSH connection: {e}", flush=True)
    
    def close_all(self):
        """Close all SSH connections."""
        for connection_key in list(self.connections.keys()):
            try:
                self.connections[connection_key].close()
                del self.connections[connection_key]
            except Exception:
                pass
        print(f"[SSH] Closed all connections", flush=True)


# Global SSH service instance
_ssh_service = SSHService()


def get_ssh_service() -> SSHService:
    """Get SSH service instance."""
    return _ssh_service
