/*
 * SCRUB — Sensor + GPS + Compass (STANDARD PINS: Arduino Uno <-> RPi)
 *
 * Communication: Arduino Uno HardwareSerial (D0=RX, D1=TX) <-> Raspberry Pi
 * GPIO UART (/dev/ttyAMA0). These are the standard/default serial pins.
 *
 *   ARDUINO            RASPBERRY PI
 *   D1 (TX)     --->   GPIO 15 (RXD, physical pin 10)
 *   D0 (RX)     <---   GPIO 14 (TXD, physical pin  8)
 *   GND         ---    GND
 *
 *   Baud: 115200  (matches nav_service SensorGpsLink)
 *
 * STANDARD PIN MAP (Arduino Uno):
 *   D2    DHT11 data
 *   D3    GPS  RX (UART RX -> GPS TX)   [SoftwareSerial]
 *   D4    GPS  TX (UART TX -> GPS RX)   [SoftwareSerial]  baud 9600
 *   A0    TDS sensor  (analog out)
 *   A1    pH  sensor  (analog out)
 *   A2    Turbidity   (analog out)
 *   A3    MQ135 (optional, analog out)
 *   A4    I2C SDA -> Compass (HMC5883L / QMC5883L)
 *   A5    I2C SCL -> Compass
 *
 * Output (newline JSON, 1 Hz):
 *   {"seq":n,"gps":{"lat":..,"lon":..,"alt":..,"spd":..,"course":..,"sats":..,"fix":..},"hdg":..,"sensors":{"ph":..,"tds":..,"turb":..,"wtemp":..,"atemp":..,"hum":..},"mode":"AUTO"}
 */

#include <Wire.h>
#include <SoftwareSerial.h>
#include <TinyGPSPlus.h>
#include <DHT.h>
#include <math.h>

// ---- Standard pins (Arduino Uno) ----
#define DHT_PIN         2      // DHT11
#define GPS_RX_PIN      3      // GPS RX (SoftwareSerial)
#define GPS_TX_PIN      4      // GPS TX (SoftwareSerial)

#define TDS_PIN         A0
#define PH_PIN          A1
#define TURBIDITY_PIN   A2
#define MQ_PIN          A3

#define DHT_TYPE        DHT11

#define HMC5883L_ADDR   0x1E
#define QMC5883L_ADDR   0x0D

// ---- Calibration ----
float PH_OFFSET   = 0.0;
float TDS_FACTOR  = 1.0;
float TURB_OFFSET = 0.0;

// ---- Compass calibration ----
float MAG_X_OFFSET = 0.0;
float MAG_Y_OFFSET = 0.0;
float MAG_X_SCALE  = 1.0;
float MAG_Y_SCALE  = 1.0;

DHT dht(DHT_PIN, DHT_TYPE);
SoftwareSerial gpsSerial(GPS_RX_PIN, GPS_TX_PIN);
TinyGPSPlus gps;

bool compassAvailable = false;
bool compassIsQMC = false;
float headingDegrees = 0.0;
unsigned long seq = 0;

float readAnalogAvg(int pin, int samples = 30) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delayMicroseconds(250);
  }
  return (float)sum / samples;
}

// ---- I2C helpers ----
void writeReg(uint8_t addr, uint8_t reg, uint8_t val) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

bool readRegs(uint8_t addr, uint8_t reg, uint8_t *buf, uint8_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(addr, len) != len) return false;
  for (uint8_t i = 0; i < len; i++) buf[i] = Wire.read();
  return true;
}

bool initCompass() {
  Wire.beginTransmission(QMC5883L_ADDR);
  if (Wire.endTransmission() == 0) {
    writeReg(QMC5883L_ADDR, 0x0B, 0x01);
    writeReg(QMC5883L_ADDR, 0x09, 0x1D);
    compassIsQMC = true;
    Serial.println(F("# Compass: QMC5883L"));
    return true;
  }
  Wire.beginTransmission(HMC5883L_ADDR);
  if (Wire.endTransmission() == 0) {
    writeReg(HMC5883L_ADDR, 0x00, 0x70);
    writeReg(HMC5883L_ADDR, 0x01, 0x20);
    writeReg(HMC5883L_ADDR, 0x02, 0x00);
    compassIsQMC = false;
    Serial.println(F("# Compass: HMC5883L"));
    return true;
  }
  return false;
}

bool readQMC(int16_t &x, int16_t &y, int16_t &z) {
  uint8_t d[6];
  if (!readRegs(QMC5883L_ADDR, 0x00, d, 6)) return false;
  x = (int16_t)((d[1] << 8) | d[0]);
  y = (int16_t)((d[3] << 8) | d[2]);
  z = (int16_t)((d[5] << 8) | d[4]);
  return true;
}

bool readHMC(int16_t &x, int16_t &y, int16_t &z) {
  uint8_t d[6];
  if (!readRegs(HMC5883L_ADDR, 0x03, d, 6)) return false;
  x = (int16_t)((d[0] << 8) | d[1]);
  z = (int16_t)((d[2] << 8) | d[3]);
  y = (int16_t)((d[4] << 8) | d[5]);
  return true;
}

float compassHeading() {
  if (!compassAvailable) return 0.0;
  int16_t x, y, z;
  bool ok = compassIsQMC ? readQMC(x, y, z) : readHMC(x, y, z);
  if (!ok) return headingDegrees;
  float fx = ((float)x - MAG_X_OFFSET) * MAG_X_SCALE;
  float fy = ((float)y - MAG_Y_OFFSET) * MAG_Y_SCALE;
  float h = atan2(fy, fx) * 180.0 / PI;
  if (h < 0) h += 360.0;
  h += 0.0; // declination
  if (h < 0) h += 360.0;
  if (h >= 360.0) h -= 360.0;
  return h;
}

void processGPS() {
  while (gpsSerial.available()) gps.encode(gpsSerial.read());
}

void setup() {
  Serial.begin(115200);    // HardwareSerial D0/D1 -> Pi (/dev/ttyAMA0)
  gpsSerial.begin(9600);   // GPS
  dht.begin();
  Wire.begin();
  delay(100);
  compassAvailable = initCompass();
  if (!compassAvailable) Serial.println(F("# Compass: NOT DETECTED"));
  Serial.println(F("{\"seq\":0}"));
  delay(500);
}

void loop() {
  static unsigned long lastRead = 0, lastCompass = 0;

  processGPS();

  if (millis() - lastCompass >= 100) {
    lastCompass = millis();
    if (compassAvailable)
      headingDegrees = headingDegrees * 0.70 + compassHeading() * 0.30;
  }

  if (millis() - lastRead < 1000) return;
  lastRead = millis();
  seq++;

  // DHT11
  float hum = dht.readHumidity();
  float tempC = dht.readTemperature();
  if (isnan(hum) || isnan(tempC)) { hum = 0.0; tempC = 0.0; }

  // TDS
  float tdsV = readAnalogAvg(TDS_PIN) * 5.0 / 1023.0;
  float cf = 1.0 + 0.02 * (tempC - 25.0);
  if (cf <= 0) cf = 1.0;
  float tds = (133.42 * pow(tdsV / cf, 3) - 255.86 * pow(tdsV / cf, 2)
               + 857.39 * (tdsV / cf)) * 0.5 * TDS_FACTOR;
  if (tds < 0) tds = 0;

  // pH
  float phV = readAnalogAvg(PH_PIN) * 5.0 / 1023.0;
  float ph = (3.5 * phV) + PH_OFFSET;

  // Turbidity
  float turbV = readAnalogAvg(TURBIDITY_PIN) * 5.0 / 1023.0;
  float ntu = -1120.4 * sq(turbV) + 5742.3 * turbV - 4352.9 + TURB_OFFSET;
  if (ntu < 0) ntu = 0;

  // GPS
  double lat = 0, lon = 0, alt = 0, spd = 0, crs = 0;
  int sats = 0, fix = 0;
  if (gps.location.isValid()) { lat = gps.location.lat(); lon = gps.location.lng(); fix = 1; }
  if (gps.altitude.isValid()) alt = gps.altitude.meters();
  if (gps.speed.isValid())    spd = gps.speed.kmph();
  if (gps.course.isValid())   crs = gps.course.deg();
  if (gps.satellites.isValid()) sats = gps.satellites.value();

  // ---- EMIT JSON ----
  Serial.print(F("{\"seq\":")); Serial.print(seq);
  Serial.print(F(",\"gps\":{\"lat\":")); Serial.print(lat, 6);
  Serial.print(F(",\"lon\":")); Serial.print(lon, 6);
  Serial.print(F(",\"alt\":")); Serial.print(alt, 2);
  Serial.print(F(",\"spd\":")); Serial.print(spd, 2);
  Serial.print(F(",\"course\":")); Serial.print(crs, 2);
  Serial.print(F(",\"sats\":")); Serial.print(sats);
  Serial.print(F(",\"fix\":")); Serial.print(fix);
  Serial.print(F("},\"hdg\":")); Serial.print(headingDegrees, 2);
  Serial.print(F(",\"sensors\":{\"ph\":")); Serial.print(ph, 2);
  Serial.print(F(",\"tds\":")); Serial.print(tds, 2);
  Serial.print(F(",\"turb\":")); Serial.print(ntu, 2);
  Serial.print(F(",\"wtemp\":")); Serial.print(tempC, 2);
  Serial.print(F(",\"atemp\":")); Serial.print(tempC, 2);
  Serial.print(F(",\"hum\":")); Serial.print(hum, 2);
  Serial.print(F("},\"mode\":\"AUTO\""));
  Serial.println(F("}"));
}
