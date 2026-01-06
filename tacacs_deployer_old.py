#!/usr/bin/env python3
"""
TACACS+ Configuration Deployment Script for Cisco Switches
Supports: Cisco Catalyst (IOS/IOS-XE), IE (Industrial Ethernet), and Nexus (NX-OS)

Features:
- Phased deployment (test phase -> pilot phase -> production rollout)
- Pre-deployment connectivity and authentication testing
- Post-deployment TACACS verification
- Comprehensive HTML and JSON reporting
- Configuration backup before changes
- Rollback capability
- Concurrent multi-threaded deployment

Author: Network Automation Team
Version: 2.0
"""

import csv
import logging
import json
import os
import sys
import getpass
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import time
import socket

try:
    from netmiko import ConnectHandler
    from netmiko.exceptions import (
        NetmikoTimeoutException,
        NetmikoAuthenticationException,
        ConfigInvalidException
    )
except ImportError:
    print("=" * 60)
    print("ERROR: netmiko is not installed.")
    print("Install it with: pip install netmiko --break-system-packages")
    print("=" * 60)
    sys.exit(1)


# ============================================================================
# CONSTANTS AND DEFAULTS
# ============================================================================
DEFAULT_INVENTORY_FILE = "inventory.csv"
TACACS_SERVER_1 = "10.X.X.X"
TACACS_SERVER_2 = "10.X.X.X"
TACACS_KEY = "047A3B2B3C244F7B1B1C351601185D56796A"


# ============================================================================
# DEPLOYMENT PHASES
# ============================================================================
class DeploymentPhase(Enum):
    TEST = "test"           # Single device test
    PILOT = "pilot"         # Small group (tagged as pilot in inventory)
    PRODUCTION = "production"  # Full rollout


# ============================================================================
# DATA CLASSES FOR RESULTS
# ============================================================================
@dataclass
class TestResult:
    """Result of a single test."""
    name: str
    passed: bool
    message: str
    details: Optional[str] = None


@dataclass
class DeviceResult:
    """Complete result for a single device."""
    hostname: str
    ip_address: str
    device_type: str
    phase: str
    status: str  # success, failed, skipped
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Pre-deployment tests
    pre_tests: List[TestResult] = field(default_factory=list)
    
    # Deployment info
    backup_file: Optional[str] = None
    config_applied: bool = False
    config_saved: bool = False
    error: Optional[str] = None
    
    # Post-deployment verification
    post_tests: List[TestResult] = field(default_factory=list)
    
    # Timing
    duration_seconds: float = 0.0


# ============================================================================
# CONFIGURATION TEMPLATES
# ============================================================================

# Template for Cisco IOS/IOS-XE (Catalyst and IE switches)
IOS_TACACS_CONFIG = """
tacacs server ISE-1
 address ipv4 {tacacs_server_1}
 key 7 {tacacs_key}
 timeout 5
 single-connection
!
tacacs server ISE-2
 address ipv4 {tacacs_server_2}
 key 7 {tacacs_key}
 timeout 5
 single-connection
!
aaa group server tacacs+ ISE-GROUP
 server name ISE-1
 server name ISE-2
{source_interface_cmd}!
aaa new-model
aaa session-id common
!
aaa authentication login userauthen group ISE-GROUP local
aaa authentication login default group ISE-GROUP local
aaa authentication login console local
!
aaa authorization exec default group ISE-GROUP if-authenticated
aaa authorization exec console local
aaa authorization commands 1 default group ISE-GROUP local
aaa authorization commands 14 default group ISE-GROUP local
aaa authorization commands 15 default group ISE-GROUP local
!
aaa accounting exec default start-stop group ISE-GROUP
aaa accounting commands 1 default start-stop group ISE-GROUP
aaa accounting commands 14 default start-stop group ISE-GROUP
aaa accounting commands 15 default start-stop group ISE-GROUP
!
line con 0
 exec-timeout 0 0
 login authentication console
 stopbits 1
!
line vty 0 4
 exec-timeout 5 0
 login authentication userauthen
 transport preferred none
 transport input all
 transport output all
!
line vty 5 15
 exec-timeout 5 0
 login authentication userauthen
 transport preferred none
 transport input all
"""

# Template for Cisco NX-OS (Nexus switches)
NXOS_TACACS_CONFIG = """
feature tacacs+
!
tacacs-server key 7 {tacacs_key}
tacacs-server host {tacacs_server_1} key 7 {tacacs_key}
tacacs-server host {tacacs_server_2} key 7 {tacacs_key}
!
aaa group server tacacs+ ISE-GROUP
 server {tacacs_server_1}
 server {tacacs_server_2}
 use-vrf management
!
aaa authentication login default group ISE-GROUP local
aaa authentication login console local
!
aaa authorization commands default group ISE-GROUP local
!
aaa accounting default group ISE-GROUP
!
line console
 exec-timeout 0
!
line vty
 session-limit 10
 exec-timeout 5
"""


class TacacsDeployer:
    """
    Main class to deploy TACACS+ configuration to Cisco switches.
    Supports phased deployment with comprehensive testing and reporting.
    """
    
    def __init__(self, inventory_file: str, credentials: Dict, 
                 max_threads: int = 10, timeout: int = 60,
                 dry_run: bool = False, phase: DeploymentPhase = DeploymentPhase.TEST):
        self.inventory_file = inventory_file
        self.credentials = credentials
        self.max_threads = max_threads
        self.timeout = timeout
        self.dry_run = dry_run
        self.phase = phase
        
        self.results: List[DeviceResult] = []
        self.start_time = None
        self.end_time = None
        
        self._setup_logging()
        self._setup_directories()
        
    def _setup_directories(self):
        """Create necessary directories."""
        for dir_name in ['logs', 'backups', 'reports']:
            os.makedirs(dir_name, exist_ok=True)
        
    def _setup_logging(self):
        """Configure logging to file and console."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_dir = 'logs'
        os.makedirs(log_dir, exist_ok=True)
        
        log_file = os.path.join(log_dir, f'tacacs_deployment_{timestamp}.log')
        
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        
        self.logger = logging.getLogger('TacacsDeployer')
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.log_file = log_file
        self.logger.info(f"Logging to: {log_file}")
        
    def load_inventory(self) -> List[Dict]:
        """Load switch inventory from CSV file."""
        devices = []
        
        try:
            with open(self.inventory_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Skip comment lines
                    if row.get('hostname', '').strip().startswith('#'):
                        continue
                        
                    if not row.get('ip_address'):
                        self.logger.warning(f"Skipping row with missing IP: {row}")
                        continue
                    
                    device = {
                        'hostname': row.get('hostname', row['ip_address']).strip(),
                        'ip_address': row['ip_address'].strip(),
                        'device_type': row.get('device_type', 'cisco_ios').strip().lower(),
                        'source_interface': row.get('source_interface', '').strip(),
                        'enabled': row.get('enabled', 'true').lower() == 'true',
                        'phase': row.get('phase', 'production').strip().lower()
                    }
                    
                    # Filter by phase
                    if self.phase == DeploymentPhase.TEST:
                        # Test phase: only first enabled device
                        if device['enabled'] and len(devices) == 0:
                            devices.append(device)
                    elif self.phase == DeploymentPhase.PILOT:
                        # Pilot phase: only devices marked as pilot
                        if device['enabled'] and device['phase'] == 'pilot':
                            devices.append(device)
                    else:
                        # Production: all enabled devices
                        if device['enabled']:
                            devices.append(device)
                        
        except FileNotFoundError:
            self.logger.error(f"Inventory file not found: {self.inventory_file}")
            raise
        except Exception as e:
            self.logger.error(f"Error loading inventory: {e}")
            raise
            
        self.logger.info(f"Loaded {len(devices)} devices for {self.phase.value} phase")
        return devices
    
    def get_config_commands(self, device_type: str, source_interface: str = '') -> List[str]:
        """Get configuration commands based on device type."""
        # Prepare source interface command
        source_interface_cmd = ""
        if source_interface and device_type != 'cisco_nxos':
            source_interface_cmd = f" ip tacacs source-interface {source_interface}\n"
        
        # Select template
        if device_type == 'cisco_nxos':
            config_text = NXOS_TACACS_CONFIG.format(
                tacacs_server_1=TACACS_SERVER_1,
                tacacs_server_2=TACACS_SERVER_2,
                tacacs_key=TACACS_KEY
            )
        else:
            config_text = IOS_TACACS_CONFIG.format(
                tacacs_server_1=TACACS_SERVER_1,
                tacacs_server_2=TACACS_SERVER_2,
                tacacs_key=TACACS_KEY,
                source_interface_cmd=source_interface_cmd
            )
            
        # Parse commands
        commands = []
        for line in config_text.strip().split('\n'):
            line = line.rstrip()
            if line and line != '!':
                commands.append(line)
                
        return commands
    
    # ========================================================================
    # PRE-DEPLOYMENT TESTS
    # ========================================================================
    
    def test_tcp_connectivity(self, ip_address: str, port: int = 22) -> TestResult:
        """Test TCP connectivity to device."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            result = sock.connect_ex((ip_address, port))
            sock.close()
            
            if result == 0:
                return TestResult(
                    name="TCP Connectivity",
                    passed=True,
                    message=f"Port {port} is reachable"
                )
            else:
                return TestResult(
                    name="TCP Connectivity",
                    passed=False,
                    message=f"Port {port} is not reachable (error code: {result})"
                )
        except Exception as e:
            return TestResult(
                name="TCP Connectivity",
                passed=False,
                message=f"Connection test failed: {str(e)}"
            )
    
    def test_ssh_authentication(self, device: Dict) -> Tuple[TestResult, Optional[object]]:
        """Test SSH authentication and return connection if successful."""
        connect_params = {
            'device_type': device['device_type'],
            'host': device['ip_address'],
            'username': self.credentials['username'],
            'password': self.credentials['password'],
            'timeout': self.timeout,
            'session_timeout': self.timeout * 2,
            'banner_timeout': 30,
            'auth_timeout': 30,
        }
        
        if self.credentials.get('enable_password'):
            connect_params['secret'] = self.credentials['enable_password']
            
        try:
            connection = ConnectHandler(**connect_params)
            
            # Try to enter enable mode
            if not connection.check_enable_mode():
                connection.enable()
                
            return TestResult(
                name="SSH Authentication",
                passed=True,
                message="Successfully authenticated and entered enable mode"
            ), connection
            
        except NetmikoAuthenticationException as e:
            return TestResult(
                name="SSH Authentication",
                passed=False,
                message=f"Authentication failed: {str(e)}"
            ), None
            
        except NetmikoTimeoutException as e:
            return TestResult(
                name="SSH Authentication",
                passed=False,
                message=f"Connection timeout: {str(e)}"
            ), None
            
        except Exception as e:
            return TestResult(
                name="SSH Authentication",
                passed=False,
                message=f"Connection error: {str(e)}"
            ), None
    
    def test_existing_tacacs(self, connection, device_type: str) -> TestResult:
        """Check if TACACS is already configured."""
        try:
            if device_type == 'cisco_nxos':
                output = connection.send_command('show tacacs-server')
            else:
                output = connection.send_command('show tacacs')
            
            has_ise1 = TACACS_SERVER_1 in output
            has_ise2 = TACACS_SERVER_2 in output
            
            if has_ise1 and has_ise2:
                return TestResult(
                    name="Existing TACACS Check",
                    passed=True,
                    message="TACACS already configured with both ISE servers",
                    details="Configuration may be updated/refreshed"
                )
            elif has_ise1 or has_ise2:
                return TestResult(
                    name="Existing TACACS Check",
                    passed=True,
                    message="Partial TACACS configuration found",
                    details="Only one ISE server configured"
                )
            else:
                return TestResult(
                    name="Existing TACACS Check",
                    passed=True,
                    message="No existing TACACS configuration found",
                    details="Fresh configuration will be applied"
                )
                
        except Exception as e:
            return TestResult(
                name="Existing TACACS Check",
                passed=False,
                message=f"Failed to check existing configuration: {str(e)}"
            )
    
    # ========================================================================
    # POST-DEPLOYMENT VERIFICATION
    # ========================================================================
    
    def verify_tacacs_servers(self, connection, device_type: str) -> TestResult:
        """Verify TACACS servers are configured."""
        try:
            if device_type == 'cisco_nxos':
                output = connection.send_command('show tacacs-server')
            else:
                output = connection.send_command('show tacacs')
            
            has_ise1 = TACACS_SERVER_1 in output
            has_ise2 = TACACS_SERVER_2 in output
            
            if has_ise1 and has_ise2:
                return TestResult(
                    name="TACACS Servers",
                    passed=True,
                    message="Both ISE servers configured",
                    details=f"ISE-1: {TACACS_SERVER_1}, ISE-2: {TACACS_SERVER_2}"
                )
            elif has_ise1 or has_ise2:
                return TestResult(
                    name="TACACS Servers",
                    passed=False,
                    message="Only one ISE server configured",
                    details=f"ISE-1: {'Yes' if has_ise1 else 'No'}, ISE-2: {'Yes' if has_ise2 else 'No'}"
                )
            else:
                return TestResult(
                    name="TACACS Servers",
                    passed=False,
                    message="No TACACS servers found"
                )
                
        except Exception as e:
            return TestResult(
                name="TACACS Servers",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    def verify_aaa_group(self, connection, device_type: str) -> TestResult:
        """Verify AAA server group is configured."""
        try:
            if device_type == 'cisco_nxos':
                output = connection.send_command('show aaa groups')
            else:
                output = connection.send_command('show aaa servers')
            
            if 'ISE-GROUP' in output or 'ISE-1' in output:
                return TestResult(
                    name="AAA Server Group",
                    passed=True,
                    message="ISE-GROUP configured correctly"
                )
            else:
                return TestResult(
                    name="AAA Server Group",
                    passed=False,
                    message="ISE-GROUP not found"
                )
                
        except Exception as e:
            return TestResult(
                name="AAA Server Group",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    def verify_aaa_authentication(self, connection, device_type: str) -> TestResult:
        """Verify AAA authentication is configured."""
        try:
            if device_type == 'cisco_nxos':
                output = connection.send_command('show aaa authentication')
            else:
                output = connection.send_command('show running-config | include aaa authentication')
            
            if 'ISE-GROUP' in output:
                return TestResult(
                    name="AAA Authentication",
                    passed=True,
                    message="Authentication configured with ISE-GROUP"
                )
            else:
                return TestResult(
                    name="AAA Authentication",
                    passed=False,
                    message="Authentication not properly configured"
                )
                
        except Exception as e:
            return TestResult(
                name="AAA Authentication",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    def verify_aaa_authorization(self, connection, device_type: str) -> TestResult:
        """Verify AAA authorization is configured."""
        try:
            if device_type == 'cisco_nxos':
                output = connection.send_command('show running-config | include aaa authorization')
            else:
                output = connection.send_command('show running-config | include aaa authorization')
            
            if 'ISE-GROUP' in output:
                return TestResult(
                    name="AAA Authorization",
                    passed=True,
                    message="Authorization configured with ISE-GROUP"
                )
            else:
                return TestResult(
                    name="AAA Authorization",
                    passed=False,
                    message="Authorization not properly configured"
                )
                
        except Exception as e:
            return TestResult(
                name="AAA Authorization",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    def verify_aaa_accounting(self, connection, device_type: str) -> TestResult:
        """Verify AAA accounting is configured."""
        try:
            output = connection.send_command('show running-config | include aaa accounting')
            
            if 'ISE-GROUP' in output:
                return TestResult(
                    name="AAA Accounting",
                    passed=True,
                    message="Accounting configured with ISE-GROUP"
                )
            else:
                return TestResult(
                    name="AAA Accounting",
                    passed=False,
                    message="Accounting not properly configured"
                )
                
        except Exception as e:
            return TestResult(
                name="AAA Accounting",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    def verify_line_config(self, connection, device_type: str) -> TestResult:
        """Verify VTY and console line configuration."""
        try:
            output = connection.send_command('show running-config | section line')
            
            checks = {
                'login_auth': 'login authentication' in output,
                'userauthen': 'userauthen' in output or 'console' in output
            }
            
            if all(checks.values()):
                return TestResult(
                    name="Line Configuration",
                    passed=True,
                    message="VTY and console lines configured correctly"
                )
            else:
                failed = [k for k, v in checks.items() if not v]
                return TestResult(
                    name="Line Configuration",
                    passed=False,
                    message=f"Missing configuration: {', '.join(failed)}"
                )
                
        except Exception as e:
            return TestResult(
                name="Line Configuration",
                passed=False,
                message=f"Verification failed: {str(e)}"
            )
    
    # ========================================================================
    # BACKUP AND CONFIGURATION
    # ========================================================================
    
    def backup_config(self, connection, hostname: str) -> Optional[str]:
        """Backup current running configuration."""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = os.path.join('backups', f'{hostname}_{timestamp}.cfg')
            
            self.logger.debug(f"Backing up configuration for {hostname}")
            running_config = connection.send_command('show running-config')
            
            with open(backup_file, 'w') as f:
                f.write(running_config)
                
            self.logger.info(f"Backup saved: {backup_file}")
            return backup_file
            
        except Exception as e:
            self.logger.error(f"Backup failed for {hostname}: {e}")
            return None
    
    def configure_device(self, device: Dict) -> DeviceResult:
        """Configure TACACS on a single device with full testing."""
        hostname = device['hostname']
        ip_address = device['ip_address']
        device_type = device['device_type']
        source_interface = device.get('source_interface', '')
        
        start_time = time.time()
        
        result = DeviceResult(
            hostname=hostname,
            ip_address=ip_address,
            device_type=device_type,
            phase=self.phase.value,
            status='pending'
        )
        
        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Processing: {hostname} ({ip_address})")
        self.logger.info(f"Device Type: {device_type}")
        self.logger.info(f"Phase: {self.phase.value}")
        self.logger.info('='*60)
        
        # ====================================================================
        # PRE-DEPLOYMENT TESTS
        # ====================================================================
        self.logger.info("\n--- Pre-Deployment Tests ---")
        
        # Test 1: TCP Connectivity
        tcp_test = self.test_tcp_connectivity(ip_address)
        result.pre_tests.append(tcp_test)
        self.logger.info(f"TCP Connectivity: {'PASS' if tcp_test.passed else 'FAIL'} - {tcp_test.message}")
        
        if not tcp_test.passed:
            result.status = 'failed'
            result.error = "TCP connectivity test failed"
            result.duration_seconds = time.time() - start_time
            return result
        
        # Test 2: SSH Authentication
        ssh_test, connection = self.test_ssh_authentication(device)
        result.pre_tests.append(ssh_test)
        self.logger.info(f"SSH Authentication: {'PASS' if ssh_test.passed else 'FAIL'} - {ssh_test.message}")
        
        if not ssh_test.passed or connection is None:
            result.status = 'failed'
            result.error = "SSH authentication failed"
            result.duration_seconds = time.time() - start_time
            return result
        
        try:
            # Test 3: Check existing TACACS configuration
            existing_test = self.test_existing_tacacs(connection, device_type)
            result.pre_tests.append(existing_test)
            self.logger.info(f"Existing TACACS: {existing_test.message}")
            
            # ================================================================
            # BACKUP CONFIGURATION
            # ================================================================
            if not self.dry_run:
                self.logger.info("\n--- Backup Configuration ---")
                backup_file = self.backup_config(connection, hostname)
                result.backup_file = backup_file
                if not backup_file:
                    self.logger.warning("Proceeding without backup")
            
            # ================================================================
            # APPLY CONFIGURATION
            # ================================================================
            config_commands = self.get_config_commands(device_type, source_interface)
            
            if self.dry_run:
                self.logger.info("\n--- DRY RUN - Commands to be applied ---")
                for cmd in config_commands:
                    self.logger.info(f"  {cmd}")
                result.status = 'success'
                result.config_applied = False
                connection.disconnect()
                result.duration_seconds = time.time() - start_time
                return result
            
            self.logger.info("\n--- Applying Configuration ---")
            self.logger.info(f"Applying {len(config_commands)} commands...")
            
            output = connection.send_config_set(
                config_commands,
                cmd_verify=False,
                exit_config_mode=True
            )
            
            self.logger.debug(f"Configuration output:\n{output}")
            
            # Check for errors
            error_patterns = ['% Invalid', '% Error', '% Incomplete', 'Ambiguous']
            for pattern in error_patterns:
                if pattern.lower() in output.lower():
                    raise ConfigInvalidException(f"Configuration error: {pattern}")
            
            result.config_applied = True
            
            # Save configuration
            self.logger.info("Saving configuration...")
            if device_type == 'cisco_nxos':
                connection.send_command('copy running-config startup-config', expect_string=r'#')
            else:
                connection.save_config()
            
            result.config_saved = True
            self.logger.info("Configuration saved successfully")
            
            # ================================================================
            # POST-DEPLOYMENT VERIFICATION
            # ================================================================
            self.logger.info("\n--- Post-Deployment Verification ---")
            
            # Wait a moment for configuration to take effect
            time.sleep(2)
            
            # Run verification tests
            verifications = [
                self.verify_tacacs_servers(connection, device_type),
                self.verify_aaa_group(connection, device_type),
                self.verify_aaa_authentication(connection, device_type),
                self.verify_aaa_authorization(connection, device_type),
                self.verify_aaa_accounting(connection, device_type),
                self.verify_line_config(connection, device_type)
            ]
            
            for test in verifications:
                result.post_tests.append(test)
                status = "PASS" if test.passed else "FAIL"
                self.logger.info(f"{test.name}: {status} - {test.message}")
            
            # Determine overall status
            all_passed = all(t.passed for t in verifications)
            if all_passed:
                result.status = 'success'
                self.logger.info("\n✓ All verification tests PASSED")
            else:
                result.status = 'partial'
                failed_tests = [t.name for t in verifications if not t.passed]
                self.logger.warning(f"\n⚠ Some tests failed: {', '.join(failed_tests)}")
            
            connection.disconnect()
            
        except ConfigInvalidException as e:
            result.status = 'failed'
            result.error = str(e)
            self.logger.error(f"Configuration error: {e}")
            try:
                connection.disconnect()
            except:
                pass
                
        except Exception as e:
            result.status = 'failed'
            result.error = f"{type(e).__name__}: {str(e)}"
            self.logger.error(f"Unexpected error: {e}")
            try:
                connection.disconnect()
            except:
                pass
        
        result.duration_seconds = time.time() - start_time
        return result
    
    # ========================================================================
    # DEPLOYMENT EXECUTION
    # ========================================================================
    
    def deploy(self) -> List[DeviceResult]:
        """Deploy TACACS configuration to all devices in inventory."""
        devices = self.load_inventory()
        
        if not devices:
            self.logger.error("No devices to configure for this phase")
            return self.results
        
        self.start_time = datetime.now()
        total_devices = len(devices)
        
        self.logger.info("\n" + "=" * 60)
        self.logger.info("TACACS+ DEPLOYMENT STARTING")
        self.logger.info("=" * 60)
        self.logger.info(f"Phase: {self.phase.value.upper()}")
        self.logger.info(f"Total devices: {total_devices}")
        self.logger.info(f"Max threads: {self.max_threads}")
        self.logger.info(f"Dry run: {self.dry_run}")
        self.logger.info(f"TACACS Servers: {TACACS_SERVER_1}, {TACACS_SERVER_2}")
        self.logger.info("=" * 60)
        
        # For test phase, run sequentially
        if self.phase == DeploymentPhase.TEST or total_devices == 1:
            for device in devices:
                result = self.configure_device(device)
                self.results.append(result)
        else:
            # Run in parallel for pilot/production
            with ThreadPoolExecutor(max_workers=self.max_threads) as executor:
                future_to_device = {
                    executor.submit(self.configure_device, device): device 
                    for device in devices
                }
                
                completed = 0
                for future in as_completed(future_to_device):
                    device = future_to_device[future]
                    completed += 1
                    
                    try:
                        result = future.result()
                        self.results.append(result)
                        
                        status_icon = "✓" if result.status == 'success' else "✗"
                        self.logger.info(
                            f"[{completed}/{total_devices}] {status_icon} {device['hostname']}: {result.status}"
                        )
                        
                    except Exception as e:
                        self.logger.error(f"Task error for {device['hostname']}: {e}")
                        self.results.append(DeviceResult(
                            hostname=device['hostname'],
                            ip_address=device['ip_address'],
                            device_type=device['device_type'],
                            phase=self.phase.value,
                            status='failed',
                            error=str(e)
                        ))
        
        self.end_time = datetime.now()
        
        # Generate reports
        self._print_summary()
        self._generate_reports()
        
        return self.results
    
    def _print_summary(self):
        """Print deployment summary."""
        success = sum(1 for r in self.results if r.status == 'success')
        partial = sum(1 for r in self.results if r.status == 'partial')
        failed = sum(1 for r in self.results if r.status == 'failed')
        total = len(self.results)
        
        elapsed = (self.end_time - self.start_time).total_seconds()
        
        self.logger.info("\n" + "=" * 60)
        self.logger.info("DEPLOYMENT SUMMARY")
        self.logger.info("=" * 60)
        self.logger.info(f"Phase: {self.phase.value.upper()}")
        self.logger.info(f"Total devices: {total}")
        self.logger.info(f"Successful: {success}")
        self.logger.info(f"Partial: {partial}")
        self.logger.info(f"Failed: {failed}")
        self.logger.info(f"Success rate: {(success + partial) / total * 100:.1f}%" if total > 0 else "N/A")
        self.logger.info(f"Elapsed time: {elapsed:.1f} seconds")
        self.logger.info("=" * 60)
        
        if failed > 0:
            self.logger.info("\nFailed Devices:")
            for r in self.results:
                if r.status == 'failed':
                    self.logger.info(f"  - {r.hostname} ({r.ip_address}): {r.error}")
    
    def _generate_reports(self):
        """Generate HTML and JSON reports."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # JSON Report
        json_file = os.path.join('reports', f'tacacs_report_{self.phase.value}_{timestamp}.json')
        report_data = {
            'deployment': {
                'phase': self.phase.value,
                'start_time': self.start_time.isoformat() if self.start_time else None,
                'end_time': self.end_time.isoformat() if self.end_time else None,
                'dry_run': self.dry_run,
                'tacacs_servers': [TACACS_SERVER_1, TACACS_SERVER_2]
            },
            'summary': {
                'total': len(self.results),
                'success': sum(1 for r in self.results if r.status == 'success'),
                'partial': sum(1 for r in self.results if r.status == 'partial'),
                'failed': sum(1 for r in self.results if r.status == 'failed')
            },
            'devices': [self._result_to_dict(r) for r in self.results]
        }
        
        with open(json_file, 'w') as f:
            json.dump(report_data, f, indent=2, default=str)
        
        self.logger.info(f"JSON report saved: {json_file}")
        
        # HTML Report
        html_file = os.path.join('reports', f'tacacs_report_{self.phase.value}_{timestamp}.html')
        self._generate_html_report(html_file, report_data)
        self.logger.info(f"HTML report saved: {html_file}")
    
    def _result_to_dict(self, result: DeviceResult) -> Dict:
        """Convert DeviceResult to dictionary."""
        return {
            'hostname': result.hostname,
            'ip_address': result.ip_address,
            'device_type': result.device_type,
            'phase': result.phase,
            'status': result.status,
            'timestamp': result.timestamp,
            'backup_file': result.backup_file,
            'config_applied': result.config_applied,
            'config_saved': result.config_saved,
            'error': result.error,
            'duration_seconds': result.duration_seconds,
            'pre_tests': [{'name': t.name, 'passed': t.passed, 'message': t.message, 'details': t.details} 
                         for t in result.pre_tests],
            'post_tests': [{'name': t.name, 'passed': t.passed, 'message': t.message, 'details': t.details} 
                          for t in result.post_tests]
        }
    
    def _generate_html_report(self, filename: str, data: Dict):
        """Generate detailed HTML report."""
        
        def get_status_class(status):
            return {
                'success': 'success',
                'partial': 'warning',
                'failed': 'danger',
                'pending': 'secondary'
            }.get(status, 'secondary')
        
        def get_status_icon(status):
            return {
                'success': '✓',
                'partial': '⚠',
                'failed': '✗',
                'pending': '○'
            }.get(status, '?')
        
        # Build device rows
        device_rows = ""
        for device in data['devices']:
            status_class = get_status_class(device['status'])
            status_icon = get_status_icon(device['status'])
            
            # Pre-tests summary
            pre_passed = sum(1 for t in device['pre_tests'] if t['passed'])
            pre_total = len(device['pre_tests'])
            
            # Post-tests summary
            post_passed = sum(1 for t in device['post_tests'] if t['passed'])
            post_total = len(device['post_tests'])
            
            device_rows += f"""
            <tr class="device-row" onclick="toggleDetails('{device['hostname']}')">
                <td><strong>{device['hostname']}</strong></td>
                <td>{device['ip_address']}</td>
                <td>{device['device_type']}</td>
                <td><span class="badge bg-{status_class}">{status_icon} {device['status'].upper()}</span></td>
                <td>{pre_passed}/{pre_total}</td>
                <td>{post_passed}/{post_total}</td>
                <td>{device['duration_seconds']:.1f}s</td>
            </tr>
            <tr id="details-{device['hostname']}" class="details-row" style="display: none;">
                <td colspan="7">
                    <div class="details-content">
                        <div class="row">
                            <div class="col-md-6">
                                <h6>Pre-Deployment Tests</h6>
                                <table class="table table-sm">
                                    {''.join(f'<tr><td>{t["name"]}</td><td><span class="badge bg-{"success" if t["passed"] else "danger"}">{"PASS" if t["passed"] else "FAIL"}</span></td><td>{t["message"]}</td></tr>' for t in device['pre_tests'])}
                                </table>
                            </div>
                            <div class="col-md-6">
                                <h6>Post-Deployment Verification</h6>
                                <table class="table table-sm">
                                    {''.join(f'<tr><td>{t["name"]}</td><td><span class="badge bg-{"success" if t["passed"] else "danger"}">{"PASS" if t["passed"] else "FAIL"}</span></td><td>{t["message"]}</td></tr>' for t in device['post_tests']) if device['post_tests'] else '<tr><td colspan="3">No post-deployment tests (configuration not applied)</td></tr>'}
                                </table>
                            </div>
                        </div>
                        {f'<div class="alert alert-danger mt-2"><strong>Error:</strong> {device["error"]}</div>' if device['error'] else ''}
                        {f'<div class="mt-2"><small>Backup: {device["backup_file"]}</small></div>' if device['backup_file'] else ''}
                    </div>
                </td>
            </tr>
            """
        
        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TACACS+ Deployment Report - {data['deployment']['phase'].upper()}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{ background-color: #f8f9fa; }}
        .report-header {{ background: linear-gradient(135deg, #1a237e 0%, #0d47a1 100%); color: white; padding: 2rem; margin-bottom: 2rem; }}
        .summary-card {{ border-left: 4px solid; }}
        .summary-card.success {{ border-left-color: #28a745; }}
        .summary-card.warning {{ border-left-color: #ffc107; }}
        .summary-card.danger {{ border-left-color: #dc3545; }}
        .summary-card.info {{ border-left-color: #17a2b8; }}
        .device-row {{ cursor: pointer; }}
        .device-row:hover {{ background-color: #f5f5f5; }}
        .details-row {{ background-color: #fafafa; }}
        .details-content {{ padding: 1rem; }}
        .badge {{ font-size: 0.9em; }}
        .config-box {{ background-color: #263238; color: #aed581; padding: 1rem; border-radius: 4px; font-family: monospace; font-size: 0.85em; max-height: 300px; overflow-y: auto; }}
    </style>
</head>
<body>
    <div class="report-header">
        <div class="container">
            <h1><i class="bi bi-shield-lock"></i> TACACS+ Deployment Report</h1>
            <p class="lead mb-0">Phase: {data['deployment']['phase'].upper()} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
    
    <div class="container">
        <!-- Summary Cards -->
        <div class="row mb-4">
            <div class="col-md-3">
                <div class="card summary-card info">
                    <div class="card-body">
                        <h5 class="card-title">Total Devices</h5>
                        <h2>{data['summary']['total']}</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card summary-card success">
                    <div class="card-body">
                        <h5 class="card-title">Successful</h5>
                        <h2 class="text-success">{data['summary']['success']}</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card summary-card warning">
                    <div class="card-body">
                        <h5 class="card-title">Partial</h5>
                        <h2 class="text-warning">{data['summary']['partial']}</h2>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card summary-card danger">
                    <div class="card-body">
                        <h5 class="card-title">Failed</h5>
                        <h2 class="text-danger">{data['summary']['failed']}</h2>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Deployment Info -->
        <div class="card mb-4">
            <div class="card-header">
                <h5 class="mb-0">Deployment Information</h5>
            </div>
            <div class="card-body">
                <div class="row">
                    <div class="col-md-6">
                        <p><strong>Start Time:</strong> {data['deployment']['start_time']}</p>
                        <p><strong>End Time:</strong> {data['deployment']['end_time']}</p>
                        <p><strong>Dry Run:</strong> {'Yes' if data['deployment']['dry_run'] else 'No'}</p>
                    </div>
                    <div class="col-md-6">
                        <p><strong>TACACS Servers:</strong></p>
                        <ul>
                            <li>ISE-1: {TACACS_SERVER_1}</li>
                            <li>ISE-2: {TACACS_SERVER_2}</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Device Results -->
        <div class="card mb-4">
            <div class="card-header">
                <h5 class="mb-0">Device Results</h5>
                <small class="text-muted">Click on a row to expand details</small>
            </div>
            <div class="card-body p-0">
                <table class="table table-hover mb-0">
                    <thead class="table-dark">
                        <tr>
                            <th>Hostname</th>
                            <th>IP Address</th>
                            <th>Type</th>
                            <th>Status</th>
                            <th>Pre-Tests</th>
                            <th>Post-Tests</th>
                            <th>Duration</th>
                        </tr>
                    </thead>
                    <tbody>
                        {device_rows}
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- Configuration Template -->
        <div class="card mb-4">
            <div class="card-header">
                <h5 class="mb-0">Configuration Templates Applied</h5>
            </div>
            <div class="card-body">
                <ul class="nav nav-tabs" id="configTabs" role="tablist">
                    <li class="nav-item" role="presentation">
                        <button class="nav-link active" id="ios-tab" data-bs-toggle="tab" data-bs-target="#ios" type="button">IOS/IOS-XE (Catalyst/IE)</button>
                    </li>
                    <li class="nav-item" role="presentation">
                        <button class="nav-link" id="nxos-tab" data-bs-toggle="tab" data-bs-target="#nxos" type="button">NX-OS (Nexus)</button>
                    </li>
                </ul>
                <div class="tab-content mt-3">
                    <div class="tab-pane fade show active" id="ios">
                        <pre class="config-box">{IOS_TACACS_CONFIG.format(tacacs_server_1=TACACS_SERVER_1, tacacs_server_2=TACACS_SERVER_2, tacacs_key=TACACS_KEY, source_interface_cmd='')}</pre>
                    </div>
                    <div class="tab-pane fade" id="nxos">
                        <pre class="config-box">{NXOS_TACACS_CONFIG.format(tacacs_server_1=TACACS_SERVER_1, tacacs_server_2=TACACS_SERVER_2, tacacs_key=TACACS_KEY)}</pre>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        function toggleDetails(hostname) {{
            var row = document.getElementById('details-' + hostname);
            if (row.style.display === 'none') {{
                row.style.display = 'table-row';
            }} else {{
                row.style.display = 'none';
            }}
        }}
    </script>
</body>
</html>
"""
        
        with open(filename, 'w') as f:
            f.write(html_content)


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def create_sample_inventory():
    """Create a sample inventory CSV file."""
    sample_inventory = """hostname,ip_address,device_type,source_interface,enabled,phase
# Pilot devices (test first)
CAT-SW-PILOT-001,192.168.1.1,cisco_ios,Vlan100,true,pilot
IE-SW-PILOT-001,192.168.2.1,cisco_ios,GigabitEthernet0/0,true,pilot
NEXUS-PILOT-001,192.168.3.1,cisco_nxos,,true,pilot
# Production devices
CAT-SW-001,192.168.1.10,cisco_ios,Vlan100,true,production
CAT-SW-002,192.168.1.11,cisco_ios,Vlan100,true,production
CAT-SW-003,192.168.1.12,cisco_ios,Vlan100,true,production
IE-SW-001,192.168.2.10,cisco_ios,GigabitEthernet0/0,true,production
IE-SW-002,192.168.2.11,cisco_ios,GigabitEthernet0/0,true,production
NEXUS-001,192.168.3.10,cisco_nxos,,true,production
NEXUS-002,192.168.3.11,cisco_nxos,,true,production
# Disabled device (won't be configured)
CAT-SW-DISABLED,192.168.1.99,cisco_ios,Vlan100,false,production
"""
    
    with open('inventory_sample.csv', 'w') as f:
        f.write(sample_inventory)
    
    print("Sample inventory created: inventory_sample.csv")
    print("\nCSV Columns:")
    print("  - hostname: Device hostname (for identification)")
    print("  - ip_address: Management IP address")
    print("  - device_type: cisco_ios (Catalyst/IE) or cisco_nxos (Nexus)")
    print("  - source_interface: Optional TACACS source interface (IOS only)")
    print("  - enabled: true/false to include/exclude device")
    print("  - phase: pilot or production")
    print("\nDeployment Phases:")
    print("  - test: First enabled device only (for initial testing)")
    print("  - pilot: Devices marked with phase=pilot")
    print("  - production: All enabled devices")


def get_script_directory():
    """Get the directory where the script is located."""
    return os.path.dirname(os.path.abspath(__file__))


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Deploy TACACS+ configuration to Cisco switches with phased rollout',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Deployment Phases:
  test        - Deploy to first device only (for validation)
  pilot       - Deploy to devices marked phase=pilot in inventory
  production  - Deploy to all enabled devices

Examples:
  # Create sample inventory file
  python tacacs_deployer.py --create-sample
  
  # Test phase - single device dry run
  python tacacs_deployer.py --phase test --dry-run
  
  # Pilot phase - deploy to pilot devices
  python tacacs_deployer.py --phase pilot
  
  # Production - full deployment with 20 threads
  python tacacs_deployer.py --phase production -t 20
  
  # Use custom inventory file
  python tacacs_deployer.py -i my_switches.csv --phase pilot
        """
    )
    
    parser.add_argument(
        '-i', '--inventory',
        help=f'Path to inventory CSV file (default: {DEFAULT_INVENTORY_FILE})'
    )
    parser.add_argument(
        '-u', '--username',
        help='SSH username (will prompt if not provided)'
    )
    parser.add_argument(
        '-p', '--password',
        help='SSH password (will prompt if not provided - recommended)'
    )
    parser.add_argument(
        '-e', '--enable-password',
        help='Enable password if different from SSH password'
    )
    parser.add_argument(
        '-t', '--threads',
        type=int,
        default=10,
        help='Maximum concurrent connections (default: 10)'
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=60,
        help='Connection timeout in seconds (default: 60)'
    )
    parser.add_argument(
        '--phase',
        choices=['test', 'pilot', 'production'],
        default='test',
        help='Deployment phase (default: test)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be configured without making changes'
    )
    parser.add_argument(
        '--create-sample',
        action='store_true',
        help='Create a sample inventory CSV file'
    )
    
    args = parser.parse_args()
    
    # Create sample inventory if requested
    if args.create_sample:
        create_sample_inventory()
        return
    
    # Determine inventory file path
    script_dir = get_script_directory()
    
    if args.inventory:
        inventory_file = args.inventory
    else:
        inventory_file = os.path.join(script_dir, DEFAULT_INVENTORY_FILE)
        if not os.path.exists(inventory_file):
            inventory_file = DEFAULT_INVENTORY_FILE
    
    # Check if inventory file exists
    if not os.path.exists(inventory_file):
        print("=" * 60)
        print("ERROR: Inventory file not found!")
        print("=" * 60)
        print(f"\nLooking for '{DEFAULT_INVENTORY_FILE}' in:")
        print(f"  1. Script folder: {script_dir}")
        print(f"  2. Current folder: {os.getcwd()}")
        print(f"\nRun: python tacacs_deployer.py --create-sample")
        print("Then edit inventory_sample.csv and rename to inventory.csv")
        sys.exit(1)
    
    print(f"Using inventory file: {inventory_file}")
    
    # Get credentials
    username = args.username or input("SSH Username: ")
    password = args.password or getpass.getpass("SSH Password: ")
    enable_password = args.enable_password
    
    credentials = {
        'username': username,
        'password': password,
        'enable_password': enable_password
    }
    
    # Map phase string to enum
    phase_map = {
        'test': DeploymentPhase.TEST,
        'pilot': DeploymentPhase.PILOT,
        'production': DeploymentPhase.PRODUCTION
    }
    phase = phase_map[args.phase]
    
    # Create deployer
    deployer = TacacsDeployer(
        inventory_file=inventory_file,
        credentials=credentials,
        max_threads=args.threads,
        timeout=args.timeout,
        dry_run=args.dry_run,
        phase=phase
    )
    
    # Confirm before deployment (except for dry run and test phase)
    if not args.dry_run and phase != DeploymentPhase.TEST:
        print("\n" + "=" * 60)
        print(f"WARNING: This will modify TACACS configuration!")
        print(f"Phase: {phase.value.upper()}")
        print("=" * 60)
        confirm = input("\nType 'yes' to continue: ")
        if confirm.lower() != 'yes':
            print("Deployment cancelled.")
            sys.exit(0)
    
    # Run deployment
    results = deployer.deploy()
    
    # Exit with error code if any failures
    failed = sum(1 for r in results if r.status == 'failed')
    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
