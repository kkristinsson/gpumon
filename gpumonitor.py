import pystray
import paramiko
from PIL import Image, ImageDraw
import threading
import time
import re
import logging
import configparser
import os

# Load config from config.ini (next to script/exe)
config = configparser.ConfigParser()
config.read('config.ini')

# Pull values from config (with defaults/fallbacks)
REMOTE_HOST = config.get('remote', 'host', fallback='localhost')
REMOTE_USER = config.get('remote', 'user', fallback='user')
REMOTE_KEY_PATH = config.get('remote', 'key_path', fallback=None)
UPDATE_INTERVAL = config.getint('app', 'update_interval', fallback=5)
HIGH_UTIL_THRESHOLD = config.getint('app', 'high_util_threshold', fallback=50)
ICON_PATH = config.get('app', 'icon_path', fallback='gpu_icon.png')
LOG_FILE = config.get('app', 'log_file', fallback='gpu_monitor.log')
ADAPTIVE_POLLING = config.getboolean('app', 'adaptive_polling', fallback=True)

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')

def validate_configuration():
    """Validate configuration values at startup"""
    errors = []

    # Check remote host
    if not REMOTE_HOST or REMOTE_HOST == 'localhost':
        errors.append("Remote host not properly configured")

    # Check SSH key path
    if REMOTE_KEY_PATH:
        key_path = os.path.expanduser(REMOTE_KEY_PATH)
        if not os.path.exists(key_path):
            errors.append(f"SSH key file not found: {key_path}")
        elif not os.access(key_path, os.R_OK):
            errors.append(f"SSH key file not readable: {key_path}")

    # Check update interval
    if UPDATE_INTERVAL < 1:
        errors.append("Update interval must be at least 1 second")

    # Check utilization threshold
    if not (0 <= HIGH_UTIL_THRESHOLD <= 100):
        errors.append("High utilization threshold must be between 0 and 100")

    # Check icon path if specified
    if ICON_PATH and not os.path.exists(ICON_PATH):
        logging.warning(f"Icon file not found: {ICON_PATH}")

    if errors:
        error_msg = "Configuration errors:\n" + "\n".join(f"- {e}" for e in errors)
        logging.error(error_msg)
        print(error_msg)
        return False

    logging.info("Configuration validation passed")
    return True

# Global SSH client for reuse
ssh_client = None
connection_retry_count = 0
max_connection_retries = 3
last_connection_attempt = 0
connection_backoff_base = 5  # Base seconds for exponential backoff

# Adaptive polling variables
last_utilization = 0
consecutive_errors = 0
fast_poll_threshold = 10  # Poll fast if utilization changes by more than 10%
adaptive_poll_enabled = ADAPTIVE_POLLING

# Generate colored icons (green for low, red for high)
def create_colored_icon(color):
    img = Image.new('RGB', (64, 64), color=color)
    draw = ImageDraw.Draw(img)
    draw.text((10, 25), "GPU", fill="white")  # Simple text if no base icon
    return img

green_icon = create_colored_icon((0, 255, 0))  # Green
red_icon = create_colored_icon((255, 0, 0))  # Red
gray_icon = create_colored_icon((128, 128, 128))  # Gray for disconnected

# Load base icon if available and tint it (relative to script dir)
try:
    base_image = Image.open(ICON_PATH)
    def tint_image(base_img, color):
        tinted = base_img.convert('RGBA')
        overlay = Image.new('RGBA', tinted.size, color + (128,))  # Semi-transparent tint
        return Image.blend(tinted, overlay, 0.5)
    green_icon = tint_image(base_image, (0, 255, 0))
    red_icon = tint_image(base_image, (255, 0, 0))
except:
    pass  # Use fallback colored squares

def get_adaptive_interval(util_pct, has_error):
    """Calculate adaptive polling interval based on utilization changes and connection stability"""
    global last_utilization, consecutive_errors

    if not adaptive_poll_enabled:
        return UPDATE_INTERVAL

    if has_error:
        consecutive_errors += 1
        # Poll more frequently when there are errors (but not too fast to avoid spam)
        return max(2, UPDATE_INTERVAL // 2)
    else:
        consecutive_errors = 0

    # If utilization changed significantly, poll faster to catch rapid changes
    util_change = abs(util_pct - last_utilization)
    if util_change > fast_poll_threshold:
        return max(1, UPDATE_INTERVAL // 3)  # Fast polling for rapid changes

    # If utilization is stable, poll slower to reduce load
    if util_change < 2:  # Very stable
        return UPDATE_INTERVAL * 2  # Poll half as often

    return UPDATE_INTERVAL  # Normal polling

def init_ssh_client():
    global ssh_client, connection_retry_count, last_connection_attempt

    # Check if we're within backoff period
    current_time = time.time()
    backoff_time = connection_backoff_base ** min(connection_retry_count, 5)  # Cap at 5 retries
    if current_time - last_connection_attempt < backoff_time:
        return f"Backing off for {backoff_time:.1f}s"

    last_connection_attempt = current_time

    if ssh_client and ssh_client.get_transport() and ssh_client.get_transport().is_active():
        return  # Already connected

    key_passphrase = None  # Start with no passphrase
    try:
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Configure connection for better stability
        ssh_client.connect(
            REMOTE_HOST,
            username=REMOTE_USER,
            key_filename=os.path.expanduser(REMOTE_KEY_PATH) if REMOTE_KEY_PATH else None,
            timeout=10,  # Connection timeout
            allow_agent=False,
            look_for_keys=False
        )

        # Enable keepalive to prevent connection drops
        transport = ssh_client.get_transport()
        transport.set_keepalive(30)  # Send keepalive every 30 seconds

        logging.info("SSH connection established with keepalive enabled")
        connection_retry_count = 0  # Reset retry count on success

    except paramiko.ssh_exception.PasswordRequiredException:
        # If passphrase required, prompt once
        key_passphrase = input("Enter key passphrase: ")
        try:
            ssh_client.connect(
                REMOTE_HOST,
                username=REMOTE_USER,
                key_filename=os.path.expanduser(REMOTE_KEY_PATH),
                password=key_passphrase,
                timeout=10,
                allow_agent=False,
                look_for_keys=False
            )
            transport = ssh_client.get_transport()
            transport.set_keepalive(30)
            logging.info("Connected with passphrase and keepalive enabled")
            connection_retry_count = 0
        except Exception as e:
            logging.error(f"SSH init error after passphrase: {str(e)}")
            if ssh_client:
                ssh_client.close()
            ssh_client = None
            connection_retry_count += 1
            return f"Auth failed: {str(e)}"

    except paramiko.ssh_exception.NoValidConnectionsError as e:
        logging.error(f"SSH connection failed - host unreachable: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        connection_retry_count += 1
        return f"Host unreachable: {REMOTE_HOST}"

    except paramiko.ssh_exception.AuthenticationException as e:
        logging.error(f"SSH authentication failed: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        connection_retry_count += 1
        return f"Authentication failed"

    except Exception as e:
        logging.error(f"SSH init error: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        connection_retry_count += 1
        return f"Connection error: {str(e)}"

def get_gpu_utilization():
    global ssh_client

    # Check connection health
    if ssh_client is None or ssh_client.get_transport() is None or not ssh_client.get_transport().is_active():
        error = init_ssh_client()
        if error:
            return f"Disconnected: {error}", 0  # Return error and default util 0

    try:
        # Use shorter timeout for GPU queries
        stdin, stdout, stderr = ssh_client.exec_command('nvidia-smi -q -x', timeout=5)
        output = stdout.read().decode('utf-8')
        error_output = stderr.read().decode('utf-8')

        if error_output.strip():
            logging.warning(f"nvidia-smi stderr: {error_output.strip()}")

        if not output.strip():
            return "No GPU data", 0

        # Parse GPU utilization using regex (more efficient than XML parsing)
        try:
            # Look for gpu_util pattern in the output - handles whitespace and formatting
            gpu_util_match = re.search(r'<gpu_util>\s*(\d+)%', output, re.IGNORECASE)
            if gpu_util_match:
                util_pct = int(gpu_util_match.group(1))
                return f"GPU: {util_pct}%", util_pct

            # Fallback: try finding any percentage after gpu_util (handles various formats)
            alt_match = re.search(r'gpu_util[^>]*>(\d+)', output, re.IGNORECASE)
            if alt_match:
                util_pct = int(alt_match.group(1))
                return f"GPU: {util_pct}%", util_pct

            logging.warning("Could not find GPU utilization in nvidia-smi output")
            return "GPU data not found", 0

        except Exception as e:
            logging.error(f"GPU utilization parse error: {str(e)}")
            return f"Parse error: {str(e)}", 0

    except paramiko.ssh_exception.SSHException as e:
        logging.error(f"SSH command error: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        return f"SSH error: {str(e)}", 0

    except Exception as e:
        logging.error(f"GPU query error: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        return f"Query error: {str(e)}", 0

def update_tooltip(icon):
    global last_utilization
    while True:
        util_text, util_pct = get_gpu_utilization()
        has_error = util_pct == 0 and ("Error" in util_text or "Disconnected" in util_text)

        # Update global state for adaptive polling
        last_utilization = util_pct

        # Truncate tooltip to 128 chars max for Windows compatibility
        icon.title = util_text[:128] if len(util_text) > 128 else util_text

        # Change icon color based on state
        if has_error:
            icon.icon = gray_icon  # Disconnected/offline state
        else:
            icon.icon = red_icon if util_pct > HIGH_UTIL_THRESHOLD else green_icon

        # Use adaptive polling interval
        sleep_time = get_adaptive_interval(util_pct, has_error)
        time.sleep(sleep_time)

def on_quit(icon, item):
    if ssh_client:
        ssh_client.close()
    icon.stop()

def on_refresh(icon, item):
    util_text, util_pct = get_gpu_utilization()
    icon.title = util_text
    icon.icon = red_icon if util_pct > HIGH_UTIL_THRESHOLD else green_icon

# Create menu
menu = (
    pystray.MenuItem('Refresh', on_refresh),
    pystray.MenuItem('Quit', on_quit)
)

# Validate configuration at startup
if not validate_configuration():
    print("Configuration validation failed. Please check your config.ini file.")
    exit(1)

# Create tray icon with initial gray (disconnected)
icon = pystray.Icon('gpu_monitor', gray_icon, 'GPU Monitor - Connecting...', menu)
logging.info("GPU Monitor application started")

# Init SSH once at start
init_result = init_ssh_client()
if init_result:
    icon.title = f'GPU Monitor - {init_result}'
    logging.warning(f"Initial SSH connection failed: {init_result}")

# Start update thread
threading.Thread(target=update_tooltip, args=(icon,), daemon=True).start()

# Run the icon
try:
    icon.run()
except KeyboardInterrupt:
    logging.info("Application interrupted")
finally:
    if ssh_client:
        ssh_client.close()