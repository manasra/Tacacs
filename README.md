# TACACS+ Configuration Deployment Script

A comprehensive Python script for deploying TACACS+ configuration to Cisco switches with phased rollout, pre/post testing, and detailed reporting.

## Supported Devices

- **Cisco Catalyst** (IOS/IOS-XE)
- **Cisco Industrial Ethernet (IE)** switches
- **Cisco Nexus** (NX-OS)

## Features

- **Phased Deployment**: Test → Pilot → Production rollout
- **Pre-Deployment Testing**: TCP connectivity, SSH authentication, existing config check
- **Post-Deployment Verification**: TACACS servers, AAA groups, authentication, authorization, accounting
- **Configuration Backup**: Automatic backup before changes
- **Concurrent Deployment**: Multi-threaded for faster rollout
- **Comprehensive Reporting**: HTML and JSON reports
- **Dry Run Mode**: Preview changes without applying

## Requirements

```bash
pip install netmiko --break-system-packages
```

## Quick Start

### 1. Create Sample Inventory

```bash
python tacacs_deployer.py --create-sample
```

### 2. Edit Inventory File

Edit `inventory.csv` with your switch details:

```csv
hostname,ip_address,device_type,source_interface,enabled,phase
CAT-SW-001,10.1.1.1,cisco_ios,Vlan100,true,pilot
NEXUS-001,10.1.1.2,cisco_nxos,,true,production
```

### 3. Run Test Phase (Single Device)

```bash
python tacacs_deployer.py --phase test --dry-run
```

### 4. Run Pilot Phase

```bash
python tacacs_deployer.py --phase pilot
```

### 5. Run Production Phase

```bash
python tacacs_deployer.py --phase production -t 20
```

## Deployment Phases

| Phase | Description | Devices |
|-------|-------------|---------|
| `test` | Initial validation | First enabled device only |
| `pilot` | Small group test | Devices with `phase=pilot` |
| `production` | Full rollout | All enabled devices |

## Command Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `-i, --inventory` | Inventory CSV file path | `inventory.csv` |
| `-u, --username` | SSH username | (prompts) |
| `-p, --password` | SSH password | (prompts) |
| `-e, --enable-password` | Enable password | (same as SSH) |
| `-t, --threads` | Max concurrent connections | 10 |
| `--timeout` | Connection timeout (seconds) | 60 |
| `--phase` | Deployment phase | test |
| `--dry-run` | Preview without changes | False |
| `--create-sample` | Create sample inventory | - |

## Inventory CSV Format

| Column | Description | Values |
|--------|-------------|--------|
| `hostname` | Device name | Any string |
| `ip_address` | Management IP | Valid IP |
| `device_type` | Platform type | `cisco_ios`, `cisco_nxos` |
| `source_interface` | TACACS source (IOS only) | Interface name |
| `enabled` | Include in deployment | `true`, `false` |
| `phase` | Deployment group | `pilot`, `production` |

## Output Files

```
├── logs/
│   └── tacacs_deployment_YYYYMMDD_HHMMSS.log
├── backups/
│   └── HOSTNAME_YYYYMMDD_HHMMSS.cfg
└── reports/
    ├── tacacs_report_PHASE_YYYYMMDD_HHMMSS.html
    └── tacacs_report_PHASE_YYYYMMDD_HHMMSS.json
```

## TACACS Configuration Applied

### IOS/IOS-XE (Catalyst & IE)

```
tacacs server ISE-1
 address ipv4 10.11.159.5
 key 7 047A3B2B3C244F7B1B1C351601185D56796A
 timeout 5
 single-connection

tacacs server ISE-2
 address ipv4 10.11.159.6
 key 7 047A3B2B3C244F7B1B1C351601185D56796A
 timeout 5
 single-connection

aaa group server tacacs+ ISE-GROUP
 server name ISE-1
 server name ISE-2

aaa new-model
aaa session-id common

aaa authentication login userauthen group ISE-GROUP local
aaa authentication login default group ISE-GROUP local
aaa authentication login console local

aaa authorization exec default group ISE-GROUP if-authenticated
aaa authorization exec console local
aaa authorization commands 1 default group ISE-GROUP local
aaa authorization commands 14 default group ISE-GROUP local
aaa authorization commands 15 default group ISE-GROUP local

aaa accounting exec default start-stop group ISE-GROUP
aaa accounting commands 1 default start-stop group ISE-GROUP
aaa accounting commands 14 default start-stop group ISE-GROUP
aaa accounting commands 15 default start-stop group ISE-GROUP

line con 0
 exec-timeout 0 0
 login authentication console
 stopbits 1

line vty 0 4
 exec-timeout 5 0
 login authentication userauthen
 transport preferred none
 transport input all
 transport output all

line vty 5 15
 exec-timeout 5 0
 login authentication userauthen
 transport preferred none
 transport input all
```

### NX-OS (Nexus)

```
feature tacacs+

tacacs-server key 7 047A3B2B3C244F7B1B1C351601185D56796A
tacacs-server host 10.11.159.5 key 7 047A3B2B3C244F7B1B1C351601185D56796A
tacacs-server host 10.11.159.6 key 7 047A3B2B3C244F7B1B1C351601185D56756A

aaa group server tacacs+ ISE-GROUP
 server 10.11.159.5
 server 10.11.159.6
 use-vrf management

aaa authentication login default group ISE-GROUP local
aaa authentication login console local

aaa authorization commands default group ISE-GROUP local

aaa accounting default group ISE-GROUP

line console
 exec-timeout 0

line vty
 session-limit 10
 exec-timeout 5
```

## Pre-Deployment Tests

1. **TCP Connectivity** - Port 22 reachability
2. **SSH Authentication** - Credential validation
3. **Existing TACACS Check** - Current configuration status

## Post-Deployment Verification

1. **TACACS Servers** - ISE-1 and ISE-2 configured
2. **AAA Server Group** - ISE-GROUP exists
3. **AAA Authentication** - Login methods configured
4. **AAA Authorization** - Command authorization enabled
5. **AAA Accounting** - Accounting enabled
6. **Line Configuration** - VTY and console lines configured

## Example Usage

### Full Deployment Workflow

```bash
# Step 1: Create and edit inventory
python tacacs_deployer.py --create-sample
mv inventory_sample.csv inventory.csv
# Edit inventory.csv with your switches

# Step 2: Test with dry run on single device
python tacacs_deployer.py --phase test --dry-run -u admin

# Step 3: Deploy to single device (test phase)
python tacacs_deployer.py --phase test -u admin

# Step 4: Review reports in reports/ folder

# Step 5: Deploy to pilot devices
python tacacs_deployer.py --phase pilot -u admin

# Step 6: Full production deployment
python tacacs_deployer.py --phase production -t 20 -u admin
```

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| Connection timeout | Increase `--timeout` value |
| Authentication failed | Verify credentials, check SSH access |
| Config errors | Check device compatibility, review logs |
| Partial verification | Some AAA features may need manual review |

### Log Files

Detailed logs are saved to `logs/` directory with timestamps.

## Safety Features

- **Backup before changes** - All configs backed up to `backups/`
- **Dry run mode** - Preview without making changes
- **Phased deployment** - Test on single device first
- **Verification tests** - Confirm configuration applied correctly
- **Error detection** - Catches configuration errors during apply

## Customization

To modify TACACS settings (servers, keys, etc.), edit the constants at the top of `tacacs_deployer.py`:

```python
TACACS_SERVER_1 = "10.11.159.5"
TACACS_SERVER_2 = "10.11.159.6"
TACACS_KEY = "047A3B2B3C244F7B1B1C351601185D56796A"
```

## License

Internal use only. Modify as needed for your environment.
