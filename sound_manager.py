import os
import time

import pygame
import serial
from serial.tools import list_ports

# Change this to your Arduino COM port (example: "COM4")
SERIAL_PORT = "COM4"

# Must match Arduino Serial.begin(...)
BAUD_RATE = 115200

# Idle prompt while waiting for an NFC tap
TAP_NFC_AUDIO_PATH = r"C:/Users/astro/Downloads/tappnfc.mp3"

# Played once when charging starts
CHARGING_START_AUDIO_PATH = r"C:/Users/astro/Downloads/chargingstart.mp3"

# Played once when charging stops
CHARGING_STOP_AUDIO_PATH = r"C:/Users/astro/Downloads/chargingstops.mp3"

def choose_serial_port(configured_port: str) -> str:
    ports = [p.device for p in list_ports.comports()]

    if configured_port in ports:
        return configured_port

    if ports:
        fallback = ports[0]
        print(f"Configured port {configured_port} not found. Using {fallback} instead.")
        return fallback

    raise RuntimeError(
        "No serial ports detected. Connect Arduino and set SERIAL_PORT correctly."
    )


def load_sound(path: str, label: str) -> pygame.mixer.Sound | None:
    if not os.path.exists(path):
        print(f"Warning: {label} file not found at {path}")
        return None

    try:
        return pygame.mixer.Sound(path)
    except pygame.error as exc:
        print(f"Warning: could not load {label} from {path}: {exc}")
        print("If AAC is not supported on this system, convert the file to WAV or MP3.")
        return None


def main() -> None:
    pygame.mixer.init()

    tap_nfc_sound = load_sound(TAP_NFC_AUDIO_PATH, "tap NFC")
    charging_start_sound = load_sound(CHARGING_START_AUDIO_PATH, "charging start")
    charging_stop_sound = load_sound(CHARGING_STOP_AUDIO_PATH, "charging stop")

    audio_channel = pygame.mixer.Channel(0)
    charging_active = False
    pending_idle_prompt = False

    if tap_nfc_sound is not None:
        audio_channel.play(tap_nfc_sound, loops=-1)
        print("Idle NFC prompt is playing.")
    else:
        print("Idle NFC prompt is disabled.")

    print("Opening serial port...")
    selected_port = choose_serial_port(SERIAL_PORT)
    print(f"Using serial port: {selected_port}")
    arduino = serial.Serial(selected_port, BAUD_RATE, timeout=1)

    time.sleep(2)
    print("Listening for NFC card...")
    print("Tap your card on the PN532.")

    while True:
        if not charging_active and pending_idle_prompt and not audio_channel.get_busy():
            if tap_nfc_sound is not None:
                audio_channel.play(tap_nfc_sound, loops=-1)
                print("Resumed idle NFC prompt.")
            pending_idle_prompt = False

        line = arduino.readline().decode(errors="ignore").strip()

        if line:
            print(line)

        if line.startswith("CARD_DETECTED") and not charging_active:
            charging_active = True
            pending_idle_prompt = False

            if charging_start_sound is not None:
                audio_channel.stop()
                audio_channel.play(charging_start_sound)
                print("Playing charging start sound.")
            else:
                print("Charging started, but start sound is unavailable.")

        elif line == "RELAY_ON":
            charging_active = True
            pending_idle_prompt = False

        elif line == "RELAY_OFF":
            charging_active = False
            pending_idle_prompt = True

            if charging_stop_sound is not None:
                audio_channel.stop()
                audio_channel.play(charging_stop_sound)
                print("Playing charging stop sound.")
            else:
                print("Charging stopped, but stop sound is unavailable.")


if __name__ == "__main__":
    main()