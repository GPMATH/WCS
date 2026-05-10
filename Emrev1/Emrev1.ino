#include <Wire.h>
#include <Adafruit_PN532.h>
#include <Adafruit_INA219.h>
#include <LiquidCrystal_I2C.h>
#include <ctype.h>
#include <string.h>
#include <stdlib.h>
#include <avr/pgmspace.h>

#define PN532_IRQ   2
#define PN532_RESET 3

#define RELAY_PIN   7
#define RELAY_PIN_2 6

#define LCD_ADDR 0x27
#define LCD_COLS 16
#define LCD_ROWS 2

#define RELAY_ON  LOW
#define RELAY_OFF HIGH

Adafruit_PN532 nfc(PN532_IRQ, PN532_RESET);
Adafruit_INA219 ina219;
LiquidCrystal_I2C lcd(LCD_ADDR, LCD_COLS, LCD_ROWS);

// NFC memory settings for NTAG213 / NTAG215 / NTAG216
#define NFC_START_PAGE 4
#define NFC_DATA_PAGES 4
#define NFC_DATA_SIZE  16

const uint8_t CARD_MAGIC[4] = { 'S', 'C', 'T', '1' };

enum PendingAction {
  ACTION_NONE,
  ACTION_REGISTER,
  ACTION_TOPUP,
  ACTION_BALANCE
};

PendingAction pendingAction = ACTION_NONE;

uint32_t pendingAccountId = 0;
uint16_t pendingTopupPoints = 0;
uint16_t selectedChargePoints = 50;

unsigned long lastCardTime = 0;
const unsigned long cooldownMs = 2500;

unsigned long relayOnTime = 0;
unsigned long activeRelayDurationMs = 30000;
bool relayActive = false;

unsigned long lastInaPrintTime = 0;
const unsigned long inaPrintIntervalMs = 500;

unsigned long lastLcdUpdateTime = 0;
const unsigned long lcdUpdateIntervalMs = 500;

float loadVoltage_V = 0.0;
float busVoltage_V = 0.0;
float current_mA = 0.0;
float power_mW = 0.0;

char lcdStatus[17] = "Charge 50 pts";
char serialLine[32];
uint8_t serialLineIndex = 0;

// ---------------- LCD HELPERS ----------------

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

void printLcdLineFlash(uint8_t row, const __FlashStringHelper *text) {
  lcd.setCursor(0, row);

  PGM_P p = (PGM_P)text;
  uint8_t i = 0;

  while (i < LCD_COLS) {
    char c = pgm_read_byte(p + i);

    if (c == '\0') {
      break;
    }

    lcd.print(c);
    i++;
  }

  while (i < LCD_COLS) {
    lcd.print(' ');
    i++;
  }
}

void setLcdStatus(const char *text) {
  strncpy(lcdStatus, text, LCD_COLS);
  lcdStatus[LCD_COLS] = '\0';
}

void setLcdStatusFlash(const __FlashStringHelper *text) {
  PGM_P p = (PGM_P)text;

  uint8_t i = 0;

  while (i < LCD_COLS) {
    char c = pgm_read_byte(p + i);

    if (c == '\0') {
      break;
    }

    lcdStatus[i] = c;
    i++;
  }

  lcdStatus[i] = '\0';
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

  dtostrf(loadVoltage_V, 4, 2, voltageText);
  dtostrf(current_mA, 4, 0, currentText);

  if (relayActive) {
    unsigned long elapsedMs = millis() - relayOnTime;
    unsigned long remainingMs = 0;

    if (elapsedMs < activeRelayDurationMs) {
      remainingMs = activeRelayDurationMs - elapsedMs;
    }

    int remainingSec = (remainingMs + 999) / 1000;

    snprintf(line1, sizeof(line1), "Charging: ON");
    snprintf(line2, sizeof(line2), "%02ds %sV %smA", remainingSec, voltageText, currentText);
  } else {
    snprintf(line1, sizeof(line1), "%s", lcdStatus);
    snprintf(line2, sizeof(line2), "%sV %smA", voltageText, currentText);
  }

  printLcdLine(0, line1);
  printLcdLine(1, line2);
}

// ---------------- DATA HELPERS ----------------

uint16_t readU16LE(const uint8_t *data) {
  return ((uint16_t)data[1] << 8) | data[0];
}

uint32_t readU32LE(const uint8_t *data) {
  return ((uint32_t)data[3] << 24) |
         ((uint32_t)data[2] << 16) |
         ((uint32_t)data[1] << 8)  |
         data[0];
}

void writeU16LE(uint8_t *data, uint16_t value) {
  data[0] = value & 0xFF;
  data[1] = (value >> 8) & 0xFF;
}

void writeU32LE(uint8_t *data, uint32_t value) {
  data[0] = value & 0xFF;
  data[1] = (value >> 8) & 0xFF;
  data[2] = (value >> 16) & 0xFF;
  data[3] = (value >> 24) & 0xFF;
}

uint16_t calculateChecksum(const uint8_t *data) {
  uint16_t checksum = 0xA55A;

  for (uint8_t i = 0; i < 14; i++) {
    checksum ^= data[i];
    checksum = (checksum << 1) | (checksum >> 15);
  }

  return checksum;
}

bool isValidCardData(const uint8_t *data) {
  for (uint8_t i = 0; i < 4; i++) {
    if (data[i] != CARD_MAGIC[i]) {
      return false;
    }
  }

  uint16_t storedChecksum = readU16LE(&data[14]);
  uint16_t calculatedChecksum = calculateChecksum(data);

  return storedChecksum == calculatedChecksum;
}

bool readCardRawData(uint8_t *data) {
  for (uint8_t page = 0; page < NFC_DATA_PAGES; page++) {
    uint8_t pageData[4];

    bool ok = nfc.ntag2xx_ReadPage(NFC_START_PAGE + page, pageData);

    if (!ok) {
      return false;
    }

    for (uint8_t i = 0; i < 4; i++) {
      data[(page * 4) + i] = pageData[i];
    }
  }

  return true;
}

bool writeCardRawData(uint8_t *data) {
  for (uint8_t page = 0; page < NFC_DATA_PAGES; page++) {
    bool ok = nfc.ntag2xx_WritePage(
      NFC_START_PAGE + page,
      &data[page * 4]
    );

    if (!ok) {
      return false;
    }

    delay(10);
  }

  return true;
}

void buildCardData(
  uint8_t *data,
  uint32_t accountId,
  uint16_t balance,
  uint16_t totalTopup,
  uint16_t totalUsed
) {
  memset(data, 0, NFC_DATA_SIZE);

  data[0] = CARD_MAGIC[0];
  data[1] = CARD_MAGIC[1];
  data[2] = CARD_MAGIC[2];
  data[3] = CARD_MAGIC[3];

  writeU32LE(&data[4], accountId);
  writeU16LE(&data[8], balance);
  writeU16LE(&data[10], totalTopup);
  writeU16LE(&data[12], totalUsed);

  uint16_t checksum = calculateChecksum(data);
  writeU16LE(&data[14], checksum);
}

uint32_t uidToAccountId(uint8_t *uid, uint8_t uidLength) {
  uint32_t hash = 2166136261UL;

  for (uint8_t i = 0; i < uidLength; i++) {
    hash ^= uid[i];
    hash *= 16777619UL;
  }

  if (hash == 0) {
    hash = 1;
  }

  return hash;
}

// ---------------- POINTS LOGIC ----------------

bool isValidPointValue(uint16_t points) {
  return points == 50 || points == 100 || points == 150;
}

unsigned long pointsToDurationMs(uint16_t points) {
  if (points == 50) {
    return 30000UL;
  }

  if (points == 100) {
    return 60000UL;
  }

  if (points == 150) {
    return 90000UL;
  }

  return 0;
}

void startCharging(unsigned long durationMs) {
  activeRelayDurationMs = durationMs;

  digitalWrite(RELAY_PIN, RELAY_ON);
  digitalWrite(RELAY_PIN_2, RELAY_ON);

  delay(100);

  relayOnTime = millis();
  relayActive = true;

  setLcdStatusFlash(F("Charging"));
  recoverLcd();
  updateLcd();

  Serial.print(F("RELAY_ON "));
  Serial.println(durationMs / 1000);
}

// ---------------- SERIAL COMMANDS ----------------

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("REGISTER <id>"));
  Serial.println(F("TOPUP 50/100/150"));
  Serial.println(F("SELECT 50/100/150"));
  Serial.println(F("BALANCE"));
  Serial.println(F("CANCEL"));
}

void processSerialCommand(char *line) {
  while (*line == ' ' || *line == '\t') {
    line++;
  }

  if (*line == '\0') {
    return;
  }

  for (uint8_t i = 0; line[i] != '\0'; i++) {
    line[i] = toupper(line[i]);
  }

  char *cmd = strtok(line, " \t");
  char *arg = strtok(NULL, " \t");

  if (cmd == NULL) {
    return;
  }

  if (strcmp(cmd, "HELP") == 0) {
    printHelp();
    return;
  }

  if (strcmp(cmd, "REGISTER") == 0 || strcmp(cmd, "REG") == 0) {
    if (arg == NULL) {
      Serial.println(F("ERR Missing ID"));
      return;
    }

    pendingAccountId = strtoul(arg, NULL, 10);

    if (pendingAccountId == 0) {
      Serial.println(F("ERR Bad ID"));
      return;
    }

    pendingAction = ACTION_REGISTER;
    setLcdStatusFlash(F("Tap register"));
    updateLcd();

    Serial.print(F("OK Tap card REG id="));
    Serial.println((unsigned long)pendingAccountId);
    return;
  }

  if (strcmp(cmd, "TOPUP") == 0) {
    if (arg == NULL) {
      Serial.println(F("ERR Missing points"));
      return;
    }

    uint16_t points = atoi(arg);

    if (!isValidPointValue(points)) {
      Serial.println(F("ERR Use 50/100/150"));
      return;
    }

    pendingTopupPoints = points;
    pendingAction = ACTION_TOPUP;

    char msg[17];
    snprintf(msg, sizeof(msg), "Topup %u", points);
    setLcdStatus(msg);
    updateLcd();

    Serial.print(F("OK Tap card TOPUP "));
    Serial.println(points);
    return;
  }

  if (strcmp(cmd, "SELECT") == 0) {
    if (arg == NULL) {
      Serial.println(F("ERR Missing selection"));
      return;
    }

    uint16_t points = atoi(arg);

    if (!isValidPointValue(points)) {
      Serial.println(F("ERR Use 50/100/150"));
      return;
    }

    selectedChargePoints = points;

    char msg[17];
    snprintf(msg, sizeof(msg), "Charge %u pts", points);
    setLcdStatus(msg);
    updateLcd();

    Serial.print(F("OK SELECT "));
    Serial.print(points);
    Serial.print(F(" pts "));
    Serial.print(pointsToDurationMs(points) / 1000);
    Serial.println(F(" s"));
    return;
  }

  if (strcmp(cmd, "BALANCE") == 0 || strcmp(cmd, "BAL") == 0) {
    pendingAction = ACTION_BALANCE;

    setLcdStatusFlash(F("Tap balance"));
    updateLcd();

    Serial.println(F("OK Tap card BAL"));
    return;
  }

  if (strcmp(cmd, "CANCEL") == 0) {
    pendingAction = ACTION_NONE;

    char msg[17];
    snprintf(msg, sizeof(msg), "Charge %u pts", selectedChargePoints);
    setLcdStatus(msg);
    updateLcd();

    Serial.println(F("OK Cancelled"));
    return;
  }

  Serial.println(F("ERR Unknown cmd"));
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      serialLine[serialLineIndex] = '\0';
      processSerialCommand(serialLine);
      serialLineIndex = 0;
    } else {
      if (serialLineIndex < sizeof(serialLine) - 1) {
        serialLine[serialLineIndex++] = c;
      }
    }
  }
}

// ---------------- CARD ACTIONS ----------------

void printUid(uint8_t *uid, uint8_t uidLength) {
  Serial.print(F("CARD UID:"));

  for (uint8_t i = 0; i < uidLength; i++) {
    Serial.print(' ');

    if (uid[i] < 0x10) {
      Serial.print('0');
    }

    Serial.print(uid[i], HEX);
  }

  Serial.println();
}

void handleRegisterCard() {
  uint8_t data[NFC_DATA_SIZE];

  buildCardData(data, pendingAccountId, 0, 0, 0);

  bool ok = writeCardRawData(data);

  if (!ok) {
    Serial.println(F("ERR Write failed"));
    setLcdStatusFlash(F("Write failed"));
    updateLcd();
    return;
  }

  Serial.print(F("OK REGISTERED id="));
  Serial.println((unsigned long)pendingAccountId);

  setLcdStatusFlash(F("Registered OK"));
  updateLcd();

  pendingAction = ACTION_NONE;
}

void handleTopupCard(uint8_t *uid, uint8_t uidLength) {
  uint8_t data[NFC_DATA_SIZE];

  uint32_t accountId = 0;
  uint16_t balance = 0;
  uint16_t totalTopup = 0;
  uint16_t totalUsed = 0;

  bool readOk = readCardRawData(data);
  bool valid = false;

  if (readOk) {
    valid = isValidCardData(data);
  }

  if (valid) {
    accountId = readU32LE(&data[4]);
    balance = readU16LE(&data[8]);
    totalTopup = readU16LE(&data[10]);
    totalUsed = readU16LE(&data[12]);
  } else {
    accountId = uidToAccountId(uid, uidLength);
    balance = 0;
    totalTopup = 0;
    totalUsed = 0;

    Serial.print(F("NEW id="));
    Serial.println((unsigned long)accountId);
  }

  if ((uint32_t)balance + pendingTopupPoints > 60000UL) {
    Serial.println(F("ERR Limit"));
    setLcdStatusFlash(F("Limit reached"));
    updateLcd();
    return;
  }

  balance += pendingTopupPoints;
  totalTopup += pendingTopupPoints;

  buildCardData(data, accountId, balance, totalTopup, totalUsed);

  bool writeOk = writeCardRawData(data);

  if (!writeOk) {
    Serial.println(F("ERR Topup write"));
    setLcdStatusFlash(F("Topup failed"));
    updateLcd();
    return;
  }

  Serial.print(F("OK TOPUP id="));
  Serial.print((unsigned long)accountId);
  Serial.print(F(" bal="));
  Serial.println(balance);

  setLcdStatusFlash(F("Topup OK"));
  updateLcd();

  pendingAction = ACTION_NONE;
}

void handleBalanceCard() {
  uint8_t data[NFC_DATA_SIZE];

  bool readOk = readCardRawData(data);

  if (!readOk) {
    Serial.println(F("ERR Read failed"));
    setLcdStatusFlash(F("Read failed"));
    updateLcd();
    return;
  }

  if (!isValidCardData(data)) {
    Serial.println(F("ERR No account"));
    setLcdStatusFlash(F("No account"));
    updateLcd();
    pendingAction = ACTION_NONE;
    return;
  }

  uint32_t accountId = readU32LE(&data[4]);
  uint16_t balance = readU16LE(&data[8]);
  uint16_t totalTopup = readU16LE(&data[10]);
  uint16_t totalUsed = readU16LE(&data[12]);

  Serial.print(F("BAL id="));
  Serial.print((unsigned long)accountId);
  Serial.print(F(" bal="));
  Serial.print(balance);
  Serial.print(F(" topup="));
  Serial.print(totalTopup);
  Serial.print(F(" used="));
  Serial.println(totalUsed);

  char msg[17];
  snprintf(msg, sizeof(msg), "Balance %u", balance);
  setLcdStatus(msg);
  updateLcd();

  pendingAction = ACTION_NONE;
}

void handleChargeCard() {
  uint8_t data[NFC_DATA_SIZE];

  bool readOk = readCardRawData(data);

  if (!readOk) {
    Serial.println(F("ERR Read card"));
    setLcdStatusFlash(F("Read failed"));
    updateLcd();
    return;
  }

  if (!isValidCardData(data)) {
    Serial.println(F("ERR No account"));
    setLcdStatusFlash(F("No account"));
    updateLcd();
    return;
  }

  uint32_t accountId = readU32LE(&data[4]);
  uint16_t balance = readU16LE(&data[8]);
  uint16_t totalTopup = readU16LE(&data[10]);
  uint16_t totalUsed = readU16LE(&data[12]);

  unsigned long durationMs = pointsToDurationMs(selectedChargePoints);

  if (balance < selectedChargePoints) {
    Serial.print(F("ERR Low bal id="));
    Serial.print((unsigned long)accountId);
    Serial.print(F(" bal="));
    Serial.println(balance);

    setLcdStatusFlash(F("Low balance"));
    updateLcd();
    return;
  }

  balance -= selectedChargePoints;
  totalUsed += selectedChargePoints;

  buildCardData(data, accountId, balance, totalTopup, totalUsed);

  bool writeOk = writeCardRawData(data);

  if (!writeOk) {
    Serial.println(F("ERR Balance write"));
    setLcdStatusFlash(F("Write failed"));
    updateLcd();
    return;
  }

  Serial.print(F("OK CHARGE id="));
  Serial.print((unsigned long)accountId);
  Serial.print(F(" used="));
  Serial.print(selectedChargePoints);
  Serial.print(F(" bal="));
  Serial.print(balance);
  Serial.print(F(" sec="));
  Serial.println(durationMs / 1000);

  startCharging(durationMs);
}

void handleCard(uint8_t *uid, uint8_t uidLength) {
  printUid(uid, uidLength);

  if (relayActive) {
    Serial.println(F("BUSY"));
    return;
  }

  if (pendingAction == ACTION_REGISTER) {
    handleRegisterCard();
    return;
  }

  if (pendingAction == ACTION_TOPUP) {
    handleTopupCard(uid, uidLength);
    return;
  }

  if (pendingAction == ACTION_BALANCE) {
    handleBalanceCard();
    return;
  }

  handleChargeCard();
}

// ---------------- SETUP ----------------

void setup() {
  Serial.begin(115200);

  pinMode(RELAY_PIN, OUTPUT);
  pinMode(RELAY_PIN_2, OUTPUT);

  digitalWrite(RELAY_PIN, RELAY_OFF);
  digitalWrite(RELAY_PIN_2, RELAY_OFF);

  delay(500);

  Serial.println(F("NFC charger prototype"));

  Wire.begin();

  lcd.init();
  lcd.backlight();
  lcd.clear();

  printLcdLineFlash(0, F("Starting..."));
  printLcdLineFlash(1, F("Please wait"));

  nfc.begin();

  uint32_t versiondata = nfc.getFirmwareVersion();

  if (!versiondata) {
    Serial.println(F("ERR PN532"));
    printLcdLineFlash(0, F("ERROR:"));
    printLcdLineFlash(1, F("PN532 not found"));

    while (1) {
      delay(1000);
    }
  }

  Serial.println(F("PN532 OK"));
  nfc.SAMConfig();

  if (!ina219.begin()) {
    Serial.println(F("ERR INA219"));
    printLcdLineFlash(0, F("ERROR:"));
    printLcdLineFlash(1, F("INA219 not found"));

    while (1) {
      delay(1000);
    }
  }

  Serial.println(F("INA219 OK"));

  setLcdStatusFlash(F("Charge 50 pts"));

  recoverLcd();
  updateLcd();

  Serial.println(F("Ready"));
  printHelp();
}

// ---------------- LOOP ----------------

void loop() {
  readSerialCommands();

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
      handleCard(uid, uidLength);
    }
  }

  if (relayActive && millis() - relayOnTime >= activeRelayDurationMs) {
    digitalWrite(RELAY_PIN, RELAY_OFF);
    digitalWrite(RELAY_PIN_2, RELAY_OFF);

    delay(100);

    relayActive = false;

    char msg[17];
    snprintf(msg, sizeof(msg), "Charge %u pts", selectedChargePoints);
    setLcdStatus(msg);

    recoverLcd();
    updateLcd();

    Serial.println(F("RELAY_OFF"));
  }

  if (millis() - lastInaPrintTime >= inaPrintIntervalMs) {
    lastInaPrintTime = millis();

    float shuntVoltage_mV = ina219.getShuntVoltage_mV();
    busVoltage_V = ina219.getBusVoltage_V();
    current_mA = ina219.getCurrent_mA();
    power_mW = ina219.getPower_mW();

    loadVoltage_V = busVoltage_V + (shuntVoltage_mV / 1000.0);

    Serial.print(F("INA V="));
    Serial.print(loadVoltage_V, 2);
    Serial.print(F(" I="));
    Serial.print(current_mA, 0);
    Serial.print(F(" R="));

    if (relayActive) {
      Serial.println(F("ON"));
    } else {
      Serial.println(F("OFF"));
    }
  }

  if (millis() - lastLcdUpdateTime >= lcdUpdateIntervalMs) {
    lastLcdUpdateTime = millis();
    updateLcd();
  }
}