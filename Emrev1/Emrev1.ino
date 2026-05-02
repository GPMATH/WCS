#include <Wire.h>
#include <Adafruit_PN532.h>
#include <Adafruit_INA219.h>
#include <LiquidCrystal_I2C.h>

#define PN532_IRQ   2
#define PN532_RESET 3

#define RELAY_PIN   7

#define LCD_ADDR 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

Adafruit_PN532 nfc(PN532_IRQ, PN532_RESET);
Adafruit_INA219 ina219;
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);

unsigned long lastCardTime = 0;
const unsigned long cooldownMs = 2500;

unsigned long relayOnTime = 0;
const unsigned long relayDurationMs = 30000; // 30 seconds

bool relayActive = false;

/*
  Many relay modules are ACTIVE LOW:
  LOW  = relay ON
  HIGH = relay OFF

  If your relay works opposite, swap these two values.
*/
#define RELAY_ON  LOW
#define RELAY_OFF HIGH

unsigned long lastInaPrintTime = 0;
const unsigned long inaPrintIntervalMs = 500;

unsigned long lastLcdUpdateTime = 0;
const unsigned long lcdUpdateIntervalMs = 500;

float loadVoltage_V = 0.0;
float busVoltage_V = 0.0;
float current_mA = 0.0;
float power_mW = 0.0;

void printLcdLine(uint8_t row, const char *text) {
  lcd.setCursor(0, row);

  uint8_t i = 0;

  while (text[i] != '\0' && i < LCD_COLS) {
    lcd.print(text[i]);
    i++;
  }

  while (i < LCD_COLS) {
    lcd.print(' ');
    i++;
  }
}

void recoverLcd() {
  delay(50);
  lcd.init();
  lcd.backlight();
  lcd.clear();
  delay(50);
}

void updateLcd() {
  char line1[17];
  char line2[17];

  char voltageText[8];
  char currentText[8];

  // Convert float values to text for Arduino Uno/Nano LCD display
  // snprintf with %.2f often does not work on AVR boards
  dtostrf(loadVoltage_V, 4, 2, voltageText); // Example: "5.02"
  dtostrf(current_mA, 4, 0, currentText);    // Example: "120"

  if (relayActive) {
    unsigned long now = millis();
    unsigned long elapsedMs = now - relayOnTime;
    unsigned long remainingMs = 0;

    if (elapsedMs < relayDurationMs) {
      remainingMs = relayDurationMs - elapsedMs;
    }

    int remainingSec = (remainingMs + 999) / 1000;

    snprintf(line1, sizeof(line1), "Charging: ON");

    // Example: "30s 5.02V 120mA"
    snprintf(
      line2,
      sizeof(line2),
      "%02ds %sV %smA",
      remainingSec,
      voltageText,
      currentText
    );
  } else {
    snprintf(line1, sizeof(line1), "Charging: OFF");

    // Shows voltage/current even when OFF
    // Example: "0.01V 0mA"
    snprintf(
      line2,
      sizeof(line2),
      "%sV %smA",
      voltageText,
      currentText
    );
  }

  printLcdLine(0, line1);
  printLcdLine(1, line2);
}

void setup() {
  Serial.begin(115200);

  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, RELAY_OFF); // Relay OFF by default

  while (!Serial) {
    delay(10);
  }

  Serial.println("PN532 NFC + relay + INA219 + LCD wireless charging monitor");

  Wire.begin();

  // Start LCD
  lcd.init();
  lcd.backlight();
  lcd.clear();

  printLcdLine(0, "Starting...");
  printLcdLine(1, "Please wait");

  // Start PN532
  nfc.begin();

  uint32_t versiondata = nfc.getFirmwareVersion();

  if (!versiondata) {
    Serial.println("ERROR: PN532 not found");
    printLcdLine(0, "ERROR:");
    printLcdLine(1, "PN532 not found");
    while (1) {
      delay(1000);
    }
  }

  Serial.println("PN532 detected");
  nfc.SAMConfig();

  // Start INA219
  if (!ina219.begin()) {
    Serial.println("ERROR: INA219 not found");
    printLcdLine(0, "ERROR:");
    printLcdLine(1, "INA219 not found");
    while (1) {
      delay(1000);
    }
  }

  Serial.println("INA219 detected");

  /*
    Optional:
    If your current is small, you can try:
    ina219.setCalibration_16V_400mA();

    If your current is higher:
    ina219.setCalibration_32V_2A();
  */

  recoverLcd();
  updateLcd();

  Serial.println("Ready. Tap NFC card...");
}

void loop() {
  uint8_t uid[7];
  uint8_t uidLength;

  bool success = nfc.readPassiveTargetID(
    PN532_MIFARE_ISO14443A,
    uid,
    &uidLength,
    100
  );

  if (success) {
    unsigned long now = millis();

    if (now - lastCardTime > cooldownMs) {
      lastCardTime = now;

      Serial.print("CARD_DETECTED UID:");

      for (uint8_t i = 0; i < uidLength; i++) {
        Serial.print(" ");
        if (uid[i] < 0x10) {
          Serial.print("0");
        }
        Serial.print(uid[i], HEX);
      }

      Serial.println();

      // Turn relay ON for 30 seconds
      digitalWrite(RELAY_PIN, RELAY_ON);
      delay(100); // gives power/noise time to settle

      relayOnTime = now;
      relayActive = true;

      recoverLcd();
      updateLcd();

      Serial.println("RELAY_ON");
    }
  }

  // Turn relay OFF after 30 seconds
  if (relayActive && millis() - relayOnTime >= relayDurationMs) {
    digitalWrite(RELAY_PIN, RELAY_OFF);
    delay(100); // gives relay/noise time to settle

    relayActive = false;

    recoverLcd();
    updateLcd();

    Serial.println("RELAY_OFF");
  }

  // Read INA219 every 500 ms
  if (millis() - lastInaPrintTime >= inaPrintIntervalMs) {
    lastInaPrintTime = millis();

    float shuntVoltage_mV = ina219.getShuntVoltage_mV();
    busVoltage_V = ina219.getBusVoltage_V();
    current_mA = ina219.getCurrent_mA();
    power_mW = ina219.getPower_mW();

    loadVoltage_V = busVoltage_V + (shuntVoltage_mV / 1000.0);

    Serial.print("INA219 | ");
    Serial.print("Load voltage: ");
    Serial.print(loadVoltage_V, 3);
    Serial.print(" V | ");

    Serial.print("Bus voltage: ");
    Serial.print(busVoltage_V, 3);
    Serial.print(" V | ");

    Serial.print("Current: ");
    Serial.print(current_mA, 2);
    Serial.print(" mA | ");

    Serial.print("Power: ");
    Serial.print(power_mW, 2);
    Serial.print(" mW | ");

    Serial.print("Relay: ");
    if (relayActive) {
      Serial.println("ON");
    } else {
      Serial.println("OFF");
    }
  }

  // Update LCD every 500 ms
  if (millis() - lastLcdUpdateTime >= lcdUpdateIntervalMs) {
    lastLcdUpdateTime = millis();
    updateLcd();
  }
}