# WCS – Wireless Charging Station Sound Manager

A Python script that listens to an Arduino/STM32 board over serial and plays audio cues in response to NFC card events and relay state changes on a wireless charging station.

## How It Works

1. On startup the script plays a looping idle prompt asking the user to tap their NFC card.
2. When the board sends `CARD_DETECTED`, the idle sound stops and a *charging start* sound plays.
3. When the board sends `RELAY_OFF`, a *charging stop* sound plays and the idle prompt resumes.

The firmware source lives in `Emrev1/Emrev1.ino`.

## Requirements

- **Python 3.10.11** – https://www.python.org/downloads/release/python-31011/
- An Arduino or STM32 board running the `Emrev1` firmware connected via USB serial
- Audio files for the three sound cues (paths configured at the top of `sound_manager.py`)

## Setup

Run the provided batch file once to create a virtual environment and install all dependencies automatically:

```bat
setup.bat
```

Or manually:

```bat
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

## Dependencies

| Package    | Version  | Purpose                        |
|------------|----------|-------------------------------|
| pygame     | >=2.5.0  | Audio playback                 |
| pyserial   | >=3.5    | Serial communication with board |

## Configuration

Edit the constants at the top of `sound_manager.py`:

| Constant                   | Default  | Description                              |
|----------------------------|----------|------------------------------------------|
| `SERIAL_PORT`              | `COM4`   | COM port the board is connected to       |
| `BAUD_RATE`                | `115200` | Must match `Serial.begin()` in firmware  |
| `TAP_NFC_AUDIO_PATH`       | —        | Path to idle/tap-prompt audio file       |
| `CHARGING_START_AUDIO_PATH`| —        | Path to charging-start audio file        |
| `CHARGING_STOP_AUDIO_PATH` | —        | Path to charging-stop audio file         |

## Running

```bat
.venv\Scripts\activate.bat
python sound_manager.py
```
