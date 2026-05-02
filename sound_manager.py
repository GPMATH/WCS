import os
import re
import threading
import time
from datetime import datetime

import matplotlib.pyplot as plt
import pygame
import serial
from serial.tools import list_ports

# Change this to your Arduino COM port (example: "COM4")
SERIAL_PORT = "COM4"

# Must match Arduino Serial.begin(...)
BAUD_RATE = 115200

# Idle prompt while waiting for an NFC tap
TAP_NFC_AUDIO_PATH = r"audio/tappnfc.mp3"

# Played once when charging starts
CHARGING_START_AUDIO_PATH = r"audio/chargingstart.mp3"

# Played once when charging stops
CHARGING_STOP_AUDIO_PATH = r"audio/chargingstops.mp3"

# Folder where verification logs/plots are stored
VERIFICATION_DIR = "verification"


def setup_verification_paths() -> tuple[str, str]:
    os.makedirs(VERIFICATION_DIR, exist_ok=True)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    measurement_log_path = os.path.join(VERIFICATION_DIR, f"verification_{run_id}.csv")
    plot_path = os.path.join(VERIFICATION_DIR, f"plots_{run_id}.png")
    return measurement_log_path, plot_path


def write_csv_headers(measurement_log_path: str) -> None:
    with open(measurement_log_path, "w", encoding="utf-8") as measurement_file:
        measurement_file.write("elapsed_seconds,current,power,raw_line\n")


def parse_measurement(line: str, name: str) -> float | None:
    # Accepts text like "CURRENT: 1.23", "power=8.5W", "I 0.7", etc.
    pattern = rf"{name}\s*[:=]?\s*(-?\d+(?:\.\d+)?)"
    match = re.search(pattern, line, flags=re.IGNORECASE)
    if not match:
        return None

    try:
        return float(match.group(1))
    except ValueError:
        return None


def log_measurement(
    measurement_log_path: str,
    elapsed_seconds: float,
    current_value: float | None,
    power_value: float | None,
    raw_line: str,
) -> None:
    current_text = "" if current_value is None else f"{current_value:.6f}"
    power_text = "" if power_value is None else f"{power_value:.6f}"
    safe_raw_line = raw_line.replace('"', '""')
    with open(measurement_log_path, "a", encoding="utf-8") as measurement_file:
        measurement_file.write(
            f'{elapsed_seconds:.3f},{current_text},{power_text},"{safe_raw_line}"\n'
        )


def plot_results(
    timestamps: list[float],
    currents: list[float | None],
    powers: list[float | None],
    plot_path: str,
    show_plot: bool = False,
) -> None:
    current_points = [(t, c) for t, c in zip(timestamps, currents) if c is not None]
    power_points = [(t, p) for t, p in zip(timestamps, powers) if p is not None]

    if not current_points and not power_points:
        print("No current/power data available to plot.")
        return

    fig, axes = plt.subplots(2, 1, figsize=(10, 8), sharex=True)

    if current_points:
        x_current, y_current = zip(*current_points)
        axes[0].plot(x_current, y_current, color="tab:blue", linewidth=1.8)
    axes[0].set_title("Current vs Time")
    axes[0].set_ylabel("Current")
    axes[0].grid(True, alpha=0.3)

    if power_points:
        x_power, y_power = zip(*power_points)
        axes[1].plot(x_power, y_power, color="tab:orange", linewidth=1.8)
    axes[1].set_title("Power vs Time")
    axes[1].set_xlabel("Time (seconds)")
    axes[1].set_ylabel("Power")
    axes[1].grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(plot_path, dpi=150)
    print(f"Saved plots to: {plot_path}")
    if show_plot:
        plt.show(block=False)
    else:
        plt.close(fig)


def start_async_plot(
    timestamps: list[float],
    currents: list[float | None],
    powers: list[float | None],
    plot_path: str,
) -> None:
    thread = threading.Thread(
        target=plot_results,
        args=(timestamps, currents, powers, plot_path),
        kwargs={"show_plot": False},
        daemon=True,
    )
    thread.start()

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

    print(f"Verification logs folder: {VERIFICATION_DIR}")

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

    measurement_log_path: str | None = None
    plot_path: str | None = None
    capture_active = False
    capture_start_time = 0.0

    timestamps: list[float] = []
    current_samples: list[float | None] = []
    power_samples: list[float | None] = []

    time.sleep(2)
    print("Listening for NFC card...")
    print("Tap your card on the PN532.")

    try:
        while True:
            if not charging_active and pending_idle_prompt and not audio_channel.get_busy():
                if tap_nfc_sound is not None:
                    audio_channel.play(tap_nfc_sound, loops=-1)
                    print("Resumed idle NFC prompt.")
                pending_idle_prompt = False

            line = arduino.readline().decode(errors="ignore").strip()
            now = time.perf_counter()

            if line:
                print(line)

            if line.startswith("CARD_DETECTED") and not charging_active:
                charging_active = True
                capture_active = True
                capture_start_time = now
                pending_idle_prompt = False
                measurement_log_path, plot_path = setup_verification_paths()
                write_csv_headers(measurement_log_path)
                timestamps = []
                current_samples = []
                power_samples = []
                print("Verification capture started.")
                print(f"Session log: {measurement_log_path}")

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
                if capture_active:
                    capture_active = False
                    session_timestamps = timestamps.copy()
                    session_currents = current_samples.copy()
                    session_powers = power_samples.copy()
                    session_plot_path = plot_path
                    if session_plot_path is not None:
                        print("Charging completed. Plotting in background...")
                        start_async_plot(
                            session_timestamps,
                            session_currents,
                            session_powers,
                            session_plot_path,
                        )

                if charging_stop_sound is not None:
                    audio_channel.stop()
                    audio_channel.play(charging_stop_sound)
                    print("Playing charging stop sound.")
                else:
                    print("Charging stopped, but stop sound is unavailable.")

            if line and capture_active:
                elapsed_seconds = now - capture_start_time
                current_value = parse_measurement(line, "current")
                power_value = parse_measurement(line, "power")

                if measurement_log_path is not None:
                    log_measurement(
                        measurement_log_path,
                        elapsed_seconds,
                        current_value,
                        power_value,
                        line,
                    )

                if current_value is not None or power_value is not None:
                    timestamps.append(elapsed_seconds)
                    current_samples.append(current_value)
                    power_samples.append(power_value)

    except KeyboardInterrupt:
        print("\nStopping program...")
    finally:
        arduino.close()
        if capture_active and plot_path is not None:
            print("Finalizing active capture and saving plot...")
            plot_results(timestamps, current_samples, power_samples, plot_path, show_plot=False)


if __name__ == "__main__":
    main()