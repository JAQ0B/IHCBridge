# IHC MQTT Bridge

This script acts as a bridge between an IHC system and an MQTT broker, enabling Home Assistant to control and monitor IHC-based outputs and inputs via MQTT.

It connects to the [IHCServer](https://github.com/priiduonu/ihcclient/blob/main/docs/IHCServer.md) using a WebSocket and exposes all input/output states over MQTT. It also listens for MQTT commands to control outputs on the IHC system. This makes it possible to integrate older Danish IHC systems into modern smart home platforms like Home Assistant.

The bridge runs on a Raspberry Pi alongside the IHCServer and is resilient, logging all activity and reconnecting automatically in case of failures.

---

## What You'll Need

- A Raspberry Pi - I used an old Rpi 1 so any model will do
- USB to [RS485 adapter](https://www.aliexpress.com/item/1005006827649035.html?spm=a2g0o.productlist.main.10.45c56b72v77toq&algo_pvid=5df227f8-8e1a-4aaa-ae29-4907c7606909&algo_exp_id=5df227f8-8e1a-4aaa-ae29-4907c7606909-9&pdp_ext_f=%7B%22order%22%3A%221173%22%2C%22eval%22%3A%221%22%7D&pdp_npi=4%40dis%21USD%215.90%211.89%21%21%2142.30%2113.54%21%40211b619a17476910942673410e0a4b%2112000038431278151%21sea%21DK%211682950224%21X&curPageLogUid=Wak0TUa0ZOqD&utparam-url=scene%3Asearch%7Cquery_from%3A) (often available on AliExpress for ~$1, and they usually work fine)
- IHC controller
- Installed and running [IHCServer](https://github.com/priiduonu/ihcclient/blob/main/docs/IHCServer.md)
- MQTT broker (e.g., EMQX installed via the Home Assistant Add-on store)
- Home Assistant (optional, but recommended)

---

## Installation

1. **Follow these guides to get your IHC server up and running:**
   - [USB to RS485 dongle configuration](https://github.com/priiduonu/ihcclient/blob/main/docs/USBtoRS485.md)
   - [IHCServer setup guide](https://github.com/priiduonu/ihcclient/blob/main/docs/IHCServer.md)


2. **Clone this repository to the `/opt` folder:**

   ```bash
   cd /opt
   git clone https://github.com/JAQ0B/IHCBridge.git


3. **Install required Python packages**

   Run the following command to install the dependencies:

       sudo pip3 install -r requirements.txt

4. **Edit `IHCBridge.py` to match your setup**

   Open the file and change the following:

   - `self.ihc_host`: Set this to the IP address of the device running the IHCServer (usually the same Pi).
   - `self.mqtt_host`: Set this to the IP address of your MQTT broker.
   - If your MQTT credentials differ from the default, update the line:

         self.mqtt_client.username_pw_set("USERNAME", "PASSWORD")

     Or remove it if your broker doesn’t require authentication.

---

## 🔄 Daemon Setup (Auto-start on Boot)

### Create systemd service files

1. **Create the service file for the IHC MQTT Bridge**

   File path: `/etc/systemd/system/ihc-bridge.service`

       [Unit]
       Description=IHC MQTT Bridge
       After=network.target

       [Service]
       ExecStart=/usr/bin/python3 /opt/IHCBridge.py
       Restart=always
       RestartSec=10

       [Install]
       WantedBy=multi-user.target

2. **Create the service file for the IHCServer**

   File path: `/etc/systemd/system/ihcserver.service`

       [Unit]
       Description=IHC server daemon

       [Service]
       Type=simple
       ExecStart=/opt/ihcserver/ihcserver -d
       StandardOutput=null
       Restart=always
       RestartSec=2

       [Install]
       WantedBy=sysinit.target

3. **Enable and start both services**

   Run the following commands:

       sudo systemctl daemon-reexec
       sudo systemctl daemon-reload
       sudo systemctl enable ihcserver
       sudo systemctl enable ihc-bridge
       sudo systemctl start ihcserver
       sudo systemctl start ihc-bridge

4. **View the IHC Bridge log if needed**

   Use this command to tail the log file:

       tail -f /opt/ihc_bridge.log

## 🏠 Home Assistant Integration

### 1. Set up MQTT

- Install the **EMQX** broker from the Home Assistant Add-on store.
- Use the **MQTT integration** in Home Assistant to connect to your EMQX broker.

### 2. Configure Home Assistant YAML

In your `configuration.yaml` file, add:

    mqtt:
      switch: !include mqtt_switches.yaml
      light: !include mqtt_lights.yaml

This tells Home Assistant to load switches and lights from separate files for better organization.

---

### 3. Create the YAML files

Create `mqtt_switches.yaml` for switches and `mqtt_lights.yaml` for lights.

Each output (light or switch) must be manually added based on your IHC module layout.  
Here's an example:

    # Module 1, Output 2 example
    - unique_id: ihc_1_2
      name: "Dinner Table"
      state_topic: "ihc/outputState/1/2/state"
      command_topic: "ihc/output/1/2/set"
      payload_on: "ON"
      payload_off: "OFF"

#### Explanation:

- `unique_id`: Should be globally unique. I recommend the format `ihc_module_output`.
- `name`: The friendly name shown in the Home Assistant UI.
- `state_topic`: The MQTT topic the bridge uses to publish the current output state.
- `command_topic`: The topic that sends commands back to the bridge (which controls the IHC output).
- `payload_on` / `payload_off`: Strings to represent on/off (can be customized if needed).

Repeat for all relevant outputs by adjusting the module/output numbers and names.

---

### 4. Restart Home Assistant

After saving the YAML files, restart Home Assistant.  
You should now see all of your IHC devices appear as native entities in the dashboard.

---

## 📌 Notes

- The bridge reconnects automatically and handles failure cases (MQTT down, IHC server restart, etc.).
- You can also trigger IHC or Raspberry Pi restarts via MQTT by sending `"RESTART"` to:
  - `ihc/system/restart` – restarts IHC server
  - `ihc/system/pi_restart` – reboots the Pi

These can be used in Home Assistant automations or manually via MQTT.

---

## 🔒 License

This project is licensed under the **MIT License**.  
See the [LICENSE](./LICENSE) file for details.

---

## 💬 Contributions

This project is shared to help others integrate IHC with Home Assistant.  
While it’s not actively developed, **pull requests and improvements are always welcome!**
