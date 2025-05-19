import json
import time
import signal
import sys
import logging
import requests
import websocket
import subprocess
import paho.mqtt.client as mqtt
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from threading import Thread, Event

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("/opt/ihc_bridge.log"), logging.StreamHandler()],
)
logger = logging.getLogger("IHCBridge")


class IHCBridge:
    def __init__(self):
        # IHC Server settings
        self.ihc_host = "IP OF IHC SERVER"
        self.ihc_port = "8081"
        self.ihc_username = "admin"
        self.ihc_password = "12345678"

        # MQTT settings
        self.mqtt_host = "IP OF MQTT BROKER"
        self.mqtt_port = 1883

        # State variables
        self.running = False
        self.ws = None
        self.ws_thread = None
        self.ws_stop_event = Event()

        # HTTP session with retry logic
        self.session = self.create_http_session()

        # State tracking for confirmations
        self.pending_confirmations = {}  # Format: {module_output: {state, timestamp}}
        self.confirmation_failures = []  # Timestamps of recent failures
        self.last_check_time = 0
        self.confirmation_timeout = (
            10  # Timeout in seconds (increased from 5 to be safer)
        )
        self.failure_threshold = 3  # Number of failures before restart
        self.failure_window = 300  # Time window for failures (5 minutes)

        # Initialize MQTT client
        self.mqtt_client = mqtt.Client(
            client_id=f"ihc_bridge_{int(time.time())}", clean_session=True
        )
        #       self.mqtt_client.username_pw_set("USERNAME", "PASSWORD")  # Uncomment if using authentication
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        self.mqtt_client.on_disconnect = self.on_disconnect

        # Set up signal handling for graceful shutdown
        signal.signal(signal.SIGINT, self.handle_shutdown)
        signal.signal(signal.SIGTERM, self.handle_shutdown)

    def create_http_session(self):
        """Create an HTTP session with retry logic"""
        session = requests.Session()
        retries = Retry(total=5, backoff_factor=1, status_forcelist=[502, 503, 504])
        session.mount("http://", HTTPAdapter(max_retries=retries))
        return session

    def handle_shutdown(self, signum, frame):
        """Handle shutdown signals gracefully"""
        logger.info("Shutdown signal received, closing connections...")
        self.running = False
        self.ws_stop_event.set()

        if self.ws:
            self.ws.close()

        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()
        sys.exit(0)

    def connect_mqtt(self):
        """Connect to MQTT broker with retry logic"""
        retry_count = 0
        max_retries = 5
        retry_delay = 5  # seconds

        while retry_count < max_retries:
            try:
                logger.info(
                    f"Connecting to MQTT broker at {self.mqtt_host}:{self.mqtt_port}..."
                )
                self.mqtt_client.connect(self.mqtt_host, self.mqtt_port)
                logger.info("MQTT connection established")
                return True
            except Exception as e:
                retry_count += 1
                logger.error(
                    f"MQTT connection failed: {e}. Retry {retry_count}/{max_retries}"
                )
                if retry_count < max_retries:
                    time.sleep(retry_delay)
                else:
                    logger.error("Max MQTT connection retries reached")
                    return False

    def on_connect(self, client, userdata, flags, rc):
        """Handle MQTT connection"""
        if rc == 0:
            logger.info("Connected to MQTT broker")
            # Subscribe to all IHC control topics
            self.mqtt_client.subscribe("ihc/output/+/+/set")
            self.mqtt_client.subscribe("ihc/system/restart")
            self.mqtt_client.subscribe("ihc/system/pi_restart")
            logger.info("Subscribed to IHC control topics")
        else:
            logger.error(f"Failed to connect to MQTT broker, return code: {rc}")

    def on_disconnect(self, client, userdata, rc):
        """Handle MQTT disconnection"""
        logger.warning(f"Disconnected from MQTT broker with code: {rc}")
        if rc != 0 and self.running:
            logger.info("Attempting to reconnect...")
            self.connect_mqtt()

    def on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages (switch commands and system commands)"""
        try:
            topic = msg.topic
            payload = msg.payload.decode()

            # First check for system commands
            if topic == "ihc/system/restart":
                if payload.upper() == "RESTART":
                    logger.info("Received manual restart command via MQTT")
                    self.restart_ihc_server(
                        scheduled=True
                    )  # Treat as scheduled to bypass cooldown
                    # Publish confirmation
                    self.mqtt_client.publish(
                        "ihc/system/status", "Restarting IHC server", retain=False
                    )
                    return
            elif topic == "ihc/system/pi_restart":
                if payload.upper() == "RESTART":
                    logger.info("Received Raspberry Pi restart command via MQTT")
                    self.restart_raspberry_pi()
                    return

            # If not a system command, treat as output control command
            # Parse topic to get module and output numbers
            topic_parts = topic.split("/")
            if (
                len(topic_parts) != 5
                or topic_parts[0] != "ihc"
                or topic_parts[1] != "output"
                or topic_parts[4] != "set"
            ):
                logger.warning(f"Invalid topic format: {topic}")
                return

            module = topic_parts[2]
            output = topic_parts[3]
            state = payload.upper() == "ON"

            logger.info(
                f"Received command: module={module}, output={output}, state={'ON' if state else 'OFF'}"
            )

            # Send command to IHC
            self.set_ihc_output(int(module), int(output), state)
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    def set_ihc_output(self, module, output, state):
        """Send command to IHC server"""
        payload = {
            "type": "setOutput",
            "moduleNumber": module,
            "ioNumber": output,
            "state": state,
        }

        ihc_url = f"http://{self.ihc_host}:{self.ihc_port}/ihcrequest"

        try:
            logger.debug(f"Sending command to IHC: {payload}")

            # Include authentication if credentials are provided
            if self.ihc_username and self.ihc_password:
                response = self.session.post(
                    ihc_url,
                    json=payload,
                    auth=(self.ihc_username, self.ihc_password),
                    timeout=5,
                )
            else:
                response = self.session.post(ihc_url, json=payload, timeout=5)

            if response.status_code == 200:
                # Set a flag that we're waiting for confirmation
                key = f"{module}_{output}"
                self.pending_confirmations[key] = {
                    "state": state,
                    "timestamp": time.time(),
                    "module": module,
                    "output": output,
                }

                # Publish the new state back to MQTT
                topic = f"ihc/output/{module}/{output}/state"
                self.mqtt_client.publish(topic, "ON" if state else "OFF", retain=True)
                logger.info(
                    f"Successfully set output {module}/{output} to {'ON' if state else 'OFF'}"
                )
            else:
                logger.warning(
                    f"IHC server returned status code {response.status_code}: {response.text}"
                )

        except requests.exceptions.RequestException as e:
            logger.error(f"Error communicating with IHC server: {e}")

    def check_pending_confirmations(self):
        """Check if any pending confirmations have timed out"""
        current_time = time.time()
        restart_needed = False
        keys_to_remove = []

        for key, info in self.pending_confirmations.items():
            # If a confirmation has been pending for more than the timeout period
            if current_time - info["timestamp"] > self.confirmation_timeout:
                module = info["module"]
                output = info["output"]
                state = info["state"]
                logger.warning(
                    f"No state confirmation received for {module}/{output} -> {'ON' if state else 'OFF'}"
                )

                # Check if we've had multiple timeouts in a short period
                self.confirmation_failures.append(current_time)
                # Remove old failures (older than the failure window)
                self.confirmation_failures = [
                    t
                    for t in self.confirmation_failures
                    if current_time - t < self.failure_window
                ]

                # If we have enough failures in the failure window, restart the server
                if len(self.confirmation_failures) >= self.failure_threshold:
                    restart_needed = True

                keys_to_remove.append(key)

        # Remove processed items
        for key in keys_to_remove:
            del self.pending_confirmations[key]

        # Restart IHC server if needed
        if restart_needed:
            # Reset the failure counter to avoid multiple restarts
            self.confirmation_failures = []
            self.restart_ihc_server()

    def test_ihc_connection(self):
        """Test the connection to the IHC server"""
        ihc_url = f"http://{self.ihc_host}:{self.ihc_port}/ihcrequest"

        try:
            logger.info(f"Testing connection to IHC server at {ihc_url}")

            # Try a getAll request
            payload = {"type": "getAll"}

            # Include authentication if credentials are provided
            if self.ihc_username and self.ihc_password:
                response = self.session.post(
                    ihc_url,
                    json=payload,
                    auth=(self.ihc_username, self.ihc_password),
                    timeout=5,
                )
            else:
                response = self.session.post(ihc_url, json=payload, timeout=5)

            if response.status_code == 200:
                # Fetch initial states and publish to MQTT
                data = response.json()
                self.process_ihc_states(data)
                logger.info("IHC API connection successful")
                return True
            else:
                logger.error(f"IHC API returned status code {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Error testing IHC connection: {e}")
            return False

    def process_ihc_states(self, data):
        """Process IHC states and publish to MQTT"""
        try:
            # Process output modules
            output_modules = data.get("modules", {}).get("outputModules", [])
            for module in output_modules:
                if module.get("state"):  # Only process active modules
                    module_number = module.get("moduleNumber")
                    for output in module.get("outputStates", []):
                        output_number = output.get("outputNumber")
                        state = "ON" if output.get("outputState") else "OFF"
                        topic = f"ihc/output/{module_number}/{output_number}/state"
                        self.mqtt_client.publish(topic, state, retain=True)
                        logger.debug(f"Published initial state: {topic} = {state}")

            # Process input modules
            input_modules = data.get("modules", {}).get("inputModules", [])
            for module in input_modules:
                if module.get("state"):  # Only process active modules
                    module_number = module.get("moduleNumber")
                    for input_state in module.get("inputStates", []):
                        input_number = input_state.get("inputNumber")
                        state = "ON" if input_state.get("inputState") else "OFF"
                        topic = f"ihc/input/{module_number}/{input_number}/state"
                        self.mqtt_client.publish(topic, state, retain=True)
                        logger.debug(f"Published initial state: {topic} = {state}")

        except Exception as e:
            logger.error(f"Error processing IHC states: {e}")

    def websocket_worker(self):
        """WebSocket worker thread for receiving IHC events"""
        ws_url = f"ws://{self.ihc_host}:{self.ihc_port}/ihcevents-ws"

        while not self.ws_stop_event.is_set():
            try:
                logger.info(f"Connecting to IHC WebSocket at {ws_url}")
                self.ws = websocket.create_connection(ws_url)
                logger.info("WebSocket connection established")

                while not self.ws_stop_event.is_set():
                    # Set a timeout so we can check the stop event periodically
                    self.ws.settimeout(1)

                    try:
                        event = self.ws.recv()
                        json_data = json.loads(event)

                        # Handle ping-pong keepalive
                        if json_data.get("type") == "ping":
                            self.ws.send(json.dumps({"type": "pong"}))
                            continue

                        # Process state change event
                        self.process_ihc_event(json_data)

                    except websocket.WebSocketTimeoutException:
                        # This is expected due to our timeout setting
                        continue
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON from WebSocket: {e}")
                    except websocket.WebSocketException as e:
                        logger.error(f"WebSocket error: {e}")
                        break

                # Close the connection properly
                if self.ws:
                    self.ws.close()

            except Exception as e:
                logger.error(f"WebSocket connection error: {e}")

            # Don't retry immediately
            if not self.ws_stop_event.is_set():
                logger.info("WebSocket disconnected, retrying in 10 seconds...")
                time.sleep(10)

    def process_ihc_event(self, event):
        """Process an IHC event from WebSocket"""
        try:
            event_type = event.get("type")
            module_number = event.get("moduleNumber")
            io_number = event.get("ioNumber")
            state = event.get("state")

            if (
                None in (event_type, module_number, io_number, state)
                or event_type == "ping"
            ):
                return

            mqtt_state = "ON" if state else "OFF"
            topic = f"ihc/{event_type}/{module_number}/{io_number}/state"

            self.mqtt_client.publish(topic, mqtt_state, retain=True)
            logger.info(f"Published event: {topic} = {mqtt_state}")

            # Check if this is a confirmation of a pending state change
            # Important fix: Event type is "outputState" not "output"
            if event_type == "outputState":
                key = f"{module_number}_{io_number}"
                if (
                    key in self.pending_confirmations
                    and self.pending_confirmations[key]["state"] == state
                ):
                    # Confirmation received, remove from pending
                    del self.pending_confirmations[key]
                    logger.debug(
                        f"State change confirmed for {module_number}/{io_number}"
                    )

        except Exception as e:
            logger.error(f"Error processing IHC event: {e}")

    def restart_ihc_server(self, scheduled=False):
        """Restart the IHC server service"""
        logger.warning("Restarting IHC server due to repeated failures")
        try:
            # Run the restart command
            subprocess.run(["sudo", "systemctl", "restart", "ihcserver"], check=True)
            logger.info("IHC server restarted successfully")

            time.sleep(10)
            self.reset_connections()

        except subprocess.CalledProcessError as e:
            logger.error(f"Error restarting IHC server: {e}")

    def reset_connections(self):
        """Reset connections after server restart"""
        logger.info("Resetting connections after IHC server restart")

        # Close current websocket if it exists
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
            self.ws = None

        # Stop current websocket thread
        self.ws_stop_event.set()
        if self.ws_thread and self.ws_thread.is_alive():
            self.ws_thread.join(timeout=3)

        # Create and start new websocket thread
        self.ws_stop_event.clear()
        self.ws_thread = Thread(target=self.websocket_worker)
        self.ws_thread.daemon = True
        self.ws_thread.start()

        # Test connection to ensure IHC server is responsive
        retry_count = 0
        max_retries = 5

        while retry_count < max_retries:
            if self.test_ihc_connection():
                logger.info("Successfully reconnected to IHC server")
                return True

            logger.warning(
                f"Reconnection attempt {retry_count + 1}/{max_retries} failed, retrying..."
            )
            retry_count += 1
            time.sleep(5)

        logger.error("Failed to reconnect to IHC server after multiple attempts")
        return False

    def restart_raspberry_pi(self):
        """Restart the Raspberry Pi"""
        logger.warning("Initiating Raspberry Pi restart")

        try:
            # Publish status before restarting
            self.mqtt_client.publish(
                "ihc/system/status",
                "Restarting Raspberry Pi in 10 seconds...",
                retain=False,
            )

            # Give time for MQTT message to be delivered
            time.sleep(5)

            # Close connections gracefully
            self.running = False
            self.ws_stop_event.set()

            if self.ws:
                self.ws.close()

            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

            # Wait a bit more
            time.sleep(5)

            # Run the restart command
            subprocess.run(["sudo", "reboot"], check=False)

        except Exception as e:
            logger.error(f"Error restarting Raspberry Pi: {e}")
            # Try to re-establish connections if restart failed
            self.running = True
            self.reset_connections()

    def run(self):
        """Main run method"""
        # Test connection before starting
        if not self.test_ihc_connection():
            logger.error(
                "Initial connection to IHC server failed. Please check your IHC server configuration."
            )
            logger.info(
                "The bridge will still start, but it may not work correctly until the IHC server is accessible."
            )

        # Connect to MQTT broker
        if not self.connect_mqtt():
            logger.error("Failed to connect to MQTT broker. Exiting.")
            return

        self.mqtt_client.loop_start()
        self.running = True

        # Start WebSocket listener thread
        self.ws_thread = Thread(target=self.websocket_worker)
        self.ws_thread.daemon = True
        self.ws_thread.start()

        logger.info("IHC-MQTT Bridge started")

        try:
            # Keep the main thread alive
            while self.running:
                current_time = time.time()

                # Check pending confirmations every second
                if current_time - self.last_check_time >= 1:
                    self.check_pending_confirmations()
                    self.last_check_time = current_time

                time.sleep(0.1)

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")
        finally:
            logger.info("Shutting down IHC-MQTT Bridge")
            self.ws_stop_event.set()
            if self.ws_thread.is_alive():
                self.ws_thread.join(timeout=5)
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="IHC-MQTT Bridge")
    parser.add_argument(
        "--config",
        type=str,
        default="ihc_bridge_config.yaml",
        help="Path to configuration file",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    logger.info(f"Starting IHC-MQTT Bridge with config file: {args.config}")

    bridge = IHCBridge()
    bridge.run()
