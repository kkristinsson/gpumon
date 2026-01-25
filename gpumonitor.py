import pystray
import paramiko
from PIL import Image, ImageDraw
import threading
import time
import xml.etree.ElementTree as ET
from io import StringIO
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

logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format='%(asctime)s - %(message)s')

# Global SSH client for reuse
ssh_client = None

# Generate colored icons (green for low, red for high)
def create_colored_icon(color):
    img = Image.new('RGB', (64, 64), color=color)
    draw = ImageDraw.Draw(img)
    draw.text((10, 25), "GPU", fill="white")  # Simple text if no base icon
    return img

green_icon = create_colored_icon((0, 255, 0))  # Green
red_icon = create_colored_icon((255, 0, 0))  # Red

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

def init_ssh_client():
    global ssh_client
    if ssh_client and ssh_client.get_transport() and ssh_client.get_transport().is_active():
        return  # Already connected
    
    key_passphrase = None  # Start with no passphrase
    try:
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        if REMOTE_KEY_PATH:
            # Expand user path if ~ is used
            key_path = os.path.expanduser(REMOTE_KEY_PATH)
            # Try loading key with no passphrase first
            pkey = paramiko.Ed25519Key.from_private_key_file(key_path, password=key_passphrase)
            ssh_client.connect(REMOTE_HOST, username=REMOTE_USER, pkey=pkey)
            logging.info("Connected with ed25519 key auth (no passphrase).")
        
    except paramiko.ssh_exception.PasswordRequiredException:
        # If passphrase required, prompt once
        key_passphrase = input("Enter key passphrase: ")
        try:
            pkey = paramiko.Ed25519Key.from_private_key_file(key_path, password=key_passphrase)
            ssh_client.connect(REMOTE_HOST, username=REMOTE_USER, pkey=pkey)
            logging.info("Connected with ed25519 key auth (with passphrase).")
        except Exception as e:
            logging.error(f"SSH init error after passphrase: {str(e)}")
            if ssh_client:
                ssh_client.close()
            ssh_client = None
            return str(e)
    
    except Exception as e:
        logging.error(f"SSH init error: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        return str(e)

def get_gpu_utilization():
    global ssh_client
    if ssh_client is None or ssh_client.get_transport() is None or not ssh_client.get_transport().is_active():
        error = init_ssh_client()
        if error:
            return f"Error: {error}", 0  # Return error and default util 0
    
    try:
        stdin, stdout, stderr = ssh_client.exec_command('nvidia-smi -q -x')
        output = stdout.read().decode('utf-8')
        error_output = stderr.read().decode('utf-8')
        if error_output:
            logging.warning(f"nvidia-smi error: {error_output}")
        
        # Parse XML (assumes single GPU)
        root = ET.fromstring(output)
        util_str = root.find('.//utilization/gpu_util').text.strip()
        util_pct = int(util_str.replace('%', '').strip())  # Parse to int
        return f"GPU Util: {util_str}", util_pct
    
    except Exception as e:
        logging.error(f"GPU query error: {str(e)}")
        if ssh_client:
            ssh_client.close()
        ssh_client = None
        return f"Error: {str(e)}", 0

def update_tooltip(icon):
    while True:
        util_text, util_pct = get_gpu_utilization()
        icon.title = util_text
        # Change icon color based on threshold
        icon.icon = red_icon if util_pct > HIGH_UTIL_THRESHOLD else green_icon
        time.sleep(UPDATE_INTERVAL)

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

# Create tray icon with initial green
icon = pystray.Icon('gpu_monitor', green_icon, 'GPU Monitor', menu)

# Init SSH once at start
init_ssh_client()

# Start update thread
threading.Thread(target=update_tooltip, args=(icon,), daemon=True).start()

# Run the icon
icon.run()