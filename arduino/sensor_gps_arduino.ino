/*
 * SCRUB v4 — Sensor + GPS + Compass Arduino
 *
 * Raspberry Pi UART JSON protocol (matches nav_service SensorGpsLink):
 *
 * {
 *   "seq": 123,
 *   "gps":  { "lat":..., "lon":..., "alt":..., "spd":..., "course":..., "sats":..., "fix":... },
 *   "hdg":  ...,
 *   "sensors": { "ph":..., "tds":..., "turb":..., "wtemp":..., "atemp":..., "hum":... },
 *   "mode": "AUTO"
 * }
 *
 * PINOUT
 *   D2  -> DHT11 DATA
 *   D3  -> GPS RX  (Arduino receives GPS TX)
 *   D4  -> GPS TX  (Arduino sends to GPS RX)
 *   A0  -> TDS
 *   A1  -> pH
 *   A2  -> Turbidity
 *   A3  -> MQ135 (optional)
 *   A4  -> I2C SDA -> Compass
 *   A5  -> I2C SCL -> Compass
 *
 * Hardware Serial (D0/D1) -> Raspberry Pi GPIO UART (/dev/ttyAMA0):
 *   Arduino TX (D1) -> Pi GPIO 15 (RXD, pin 10)
 *   Arduino RX (D0) -> Pi GPIO 14 (TXD, pin 8)
 *   GND -> GND
 *
 * HardwareSerial = 115200 baud  (matches Pi SensorGpsLink)
 * GPS SoftwareSerial = 9600 baud
 */

#include <Wire.h>
#include <SoftwareSerial.h>
#include <TinyGPSPlus.h>
#include <DHT.h>
#include <math.h>

// ============================================================
// PIN DEFINITIONS
// ============================================================
#define DHT_PIN         2
#define GPS_RX_PIN      3
#define GPS_TX_PIN      4

#define TDS_PIN         A0
#define PH_PIN          A1
#define TURBIDITY_PIN   A2
#define MQ_PIN          A3

#define DHT_TYPE        DHT11

// ============================================================
// COMPASS I2C ADDRESSES
// ============================================================
#define HMC5883L_ADDR   0x1E
#define QMC5883L_ADDR   0x0D

// ============================================================
// CALIBRATION
// ============================================================
float PH_OFFSET   = 0.0;
float TDS_FACTOR  = 1.0;
float TURB_OFFSET = 0.0;

// ============================================================
// COMPASS CALIBRATION (calibrated = (raw - offset) * scale)
// ============================================================
float MAG_X_OFFSET = 0.0;
float MAG_Y_OFFSET = 0.0;
float MAG_X_SCALE  = 1.0;
float MAG_Y_SCALE  = 1.0;

// ============================================================
// OBJECTS
// ============================================================
DHT dht(DHT_PIN, DHT_TYPE);
SoftwareSerial gpsSerial(GPS_RX_PIN, GPS_TX_PIN);
TinyGPSPlus gps;

// ============================================================
// GLOBAL VARIABLES
// ============================================================
bool compassAvailable = false;
bool compassIsQMC = false;
float headingDegrees = 0.0;
unsigned long sequenceNumber = 0;

// ============================================================
// ANALOG FILTER
// ============================================================
float readAnalogAvg(int pin, int samples = 30) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delayMicroseconds(250);
  }
  return (float)sum / samples;
}

// ============================================================
// I2C HELPERS
// ============================================================
void writeRegister(uint8_t address, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

bool readRegisters(uint8_t address, uint8_t reg, uint8_t *buffer, uint8_t length) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0)
    return false;
  uint8_t received = Wire.requestFrom(address, length);
  if (received != length)
    return false;
  for (uint8_t i = 0; i < length; i++)
    buffer[i] = Wire.read();
  return true;
}

// ============================================================
// INITIALIZE COMPASS
// ============================================================
bool initializeCompass() {
  uint8_t buffer[2];

  // Try QMC5883L
  Wire.beginTransmission(QMC5883L_ADDR);
  if (Wire.endTransmission() == 0) {
    writeRegister(QMC5883L_ADDR, 0x0B, 0x01);   // reset
    writeRegister(QMC5883L_ADDR, 0x09, 0x1D);   // continuous, 200 Hz, 8G
    compassIsQMC = true;
    Serial.println(F("# Compass detected: QMC5883L"));
    return true;
  }

  // Try HMC5883L
  Wire.beginTransmission(HMC5883L_ADDR);
  if (Wire.endTransmission() == 0) {
    writeRegister(HMC5883L_ADDR, 0x00, 0x70);   // 8 samples, 15 Hz
    writeRegister(HMC5883L_ADDR, 0x01, 0x20);   // gain +-1.3 Ga
    writeRegister(HMC5883L_ADDR, 0x02, 0x00);   // continuous
    compassIsQMC = false;
    Serial.println(F("# Compass detected: HMC5883L"));
    return true;
  }

  return false;
}

// ============================================================
// READ COMPASS
// ============================================================
bool readQMC(int16_t &x, int16_t &y, int16_t &z) {
  uint8_t data[6];
  if (!readRegisters(QMC5883L_ADDR, 0x00, data, 6))
    return false;
  x = (int16_t)((data[1] << 8) | data[0]);
  y = (int16_t)((data[3] << 8) | data[2]);
  z = (int16_t)((data[5] << 8) | data[4]);
  return true;
}

bool readHMC(int16_t &x, int16_t &y, int16_t &z) {
  uint8_t data[6];
  if (!readRegisters(HMC5883L_ADDR, 0x03, data, 6))
    return false;
  x = (int16_t)((data[0] << 8) | data[1]);
  z = (int16_t)((data[2] << 8) | data[3]);
  y = (int16_t)((data[4] << 8) | data[5]);
  return true;
}

float readCompassHeading() {
  if (!compassAvailable)
    return 0.0;

  int16_t rawX, rawY, rawZ;
  bool success;
  if (compassIsQMC)
    success = readQMC(rawX, rawY, rawZ);
  else
    success = readHMC(rawX, rawY, rawZ);

  if (!success)
    return headingDegrees;

  float x = ((float)rawX - MAG_X_OFFSET) * MAG_X_SCALE;
  float y = ((float)rawY - MAG_Y_OFFSET) * MAG_Y_SCALE;
  float heading = atan2(y, x) * 180.0 / PI;
  if (heading < 0)
    heading += 360.0;

  float magneticDeclination = 0.0;   // set for TRUE heading if needed
  heading += magneticDeclination;
  if (heading < 0)
    heading += 360.0;
  if (heading >= 360.0)
    heading -= 360.0;
  return heading;
}

// ============================================================
// GPS
// ============================================================
void processGPS() {
  while (gpsSerial.available()) {
    char c = gpsSerial.read();
    gps.encode(c);
  }
}

// ============================================================
// SETUP
// ============================================================
void setup() {
  Serial.begin(115200);      // -> Raspberry Pi GPIO UART
  gpsSerial.begin(9600);     // -> NEO-M8N GPS
  dht.begin();
  Wire.begin();
  delay(100);

  compassAvailable = initializeCompass();
  if (compassAvailable)
    Serial.println(F("# Compass initialized"));
  else
    Serial.println(F("# WARNING: Compass not detected"));

  Serial.println(F("{\"seq\":0}"));   // preamble (ignored by Pi parser)
  delay(500);
}

// ============================================================
// LOOP
// ============================================================
void loop() {
  static unsigned long lastSensorRead = 0;
  static unsigned long lastCompassRead = 0;

  processGPS();   // MUST be called continuously

  if (millis() - lastCompassRead >= 100) {
    lastCompassRead = millis();
    if (compassAvailable) {
      float newHeading = readCompassHeading();
      headingDegrees = headingDegrees * 0.70 + newHeading * 0.30;  // smoothing
    }
  }

  if (millis() - lastSensorRead < 1000)
    return;
  lastSensorRead = millis();
  sequenceNumber++;

  // ---------- DHT11 ----------
  float humidity = dht.readHumidity();
  float tempC    = dht.readTemperature();
  if (isnan(humidity) || isnan(tempC)) { humidity = 0.0; tempC = 0.0; }

  // ---------- TDS ----------
  float tdsRaw = readAnalogAvg(TDS_PIN);
  float tdsVoltage = tdsRaw * 5.0 / 1023.0;
  float compFactor = 1.0 + 0.02 * (tempC - 25.0);
  if (compFactor <= 0) compFactor = 1.0;
  float compVoltage = tdsVoltage / compFactor;
  float tdsValue = (133.42 * pow(compVoltage, 3)
                    - 255.86 * pow(compVoltage, 2)
                    + 857.39 * compVoltage) * 0.5 * TDS_FACTOR;
  if (tdsValue < 0) tdsValue = 0;

  // ---------- pH ----------
  float phRaw = readAnalogAvg(PH_PIN);
  float phVoltage = phRaw * 5.0 / 1023.0;
  float phValue = (3.5 * phVoltage) + PH_OFFSET;

  // ---------- Turbidity ----------
  float turbRaw = readAnalogAvg(TURBIDITY_PIN);
  float turbVoltage = turbRaw * 5.0 / 1023.0;
  float ntu = -1120.4 * sq(turbVoltage) + 5742.3 * turbVoltage - 4352.9 + TURB_OFFSET;
  if (ntu < 0) ntu = 0;

  // ---------- GPS ----------
  double latitude = 0.0, longitude = 0.0, altitude = 0.0, speed = 0.0, course = 0.0;
  int satellites = 0, fix = 0;
  if (gps.location.isValid()) {
    latitude = gps.location.lat();
    longitude = gps.location.lng();
    fix = 1;
  }
  if (gps.altitude.isValid())  altitude = gps.altitude.meters();
  if (gps.speed.isValid())     speed = gps.speed.kmph();
  if (gps.course.isValid())    course = gps.course.deg();
  if (gps.satellites.isValid()) satellites = gps.satellites.value();

  // ============================================================
  // EMIT JSON
  // ============================================================
  Serial.print(F("{\"seq\":"));
  Serial.print(sequenceNumber);
  Serial.print(F(",\"gps\":{"));
  Serial.print(F("\"lat\":"));    Serial.print(latitude, 6);
  Serial.print(F(",\"lon\":"));   Serial.print(longitude, 6);
  Serial.print(F(",\"alt\":"));   Serial.print(altitude, 2);
  Serial.print(F(",\"spd\":"));   Serial.print(speed, 2);
  Serial.print(F(",\"course\":"));Serial.print(course, 2);
  Serial.print(F(",\"sats\":"));  Serial.print(satellites);
  Serial.print(F(",\"fix\":"));   Serial.print(fix);
  Serial.print(F("},\"hdg\":"));
  Serial.print(headingDegrees, 2);
  Serial.print(F(",\"sensors\":{"));
  Serial.print(F("\"ph\":"));     Serial.print(phValue, 2);
  Serial.print(F(",\"tds\":"));   Serial.print(tdsValue, 2);
  Serial.print(F(",\"turb\":"));  Serial.print(ntu, 2);
  Serial.print(F(",\"wtemp\":")); Serial.print(tempC, 2);
  Serial.print(F(",\"atemp\":")); Serial.print(tempC, 2);
  Serial.print(F(",\"hum\":"));   Serial.print(humidity, 2);
  Serial.print(F("},\"mode\":\"AUTO\""));
  Serial.println(F("}"));
}
