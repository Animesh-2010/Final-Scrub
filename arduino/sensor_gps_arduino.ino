/*
 * SCRUB v4 — Sensor + GPS Arduino (UART JSON link to Raspberry Pi)
 *
 * Speaks the nav_service SensorGpsLink wire protocol:
 *   one newline-delimited JSON line per reading, on HardwareSerial
 *   (the Pi listens on /dev/ttyAMA0 @ 115200).
 *
 * Expected keys (must match nav_service/arduino_link.py -> SensorGpsLink):
 *   seq, gps.{lat,lon,alt,spd,course,sats,fix}, hdg,
 *   sensors.{ph, tds, turb, wtemp, atemp, hum}
 *
 * GPS note: this sketch reads sensors only. The GPS NEO-M8N can be wired to
 * a SECOND serial (e.g. SoftwareSerial/UART1) and merged below — until then
 * gps fields are emitted as 0 / fix:0 so the Pi stays in a safe stale state.
 */

#include <DHT.h>

// ==================== PIN DEFINITIONS ====================
#define DHT_PIN         2       // DHT11 data -> D2
#define TDS_PIN         A0      // TDS AO -> A0
#define PH_PIN          A1      // pH AO -> A1
#define TURBIDITY_PIN   A2      // Turbidity AO -> A2
#define MQ_PIN          A3      // MQ135 AO -> A3 (extra, optional)

#define DHT_TYPE        DHT11

// ==================== CALIBRATION ====================
float PH_OFFSET       = 0.0;      // Calibrate with pH 4.0 & 7.0 buffers
float TDS_FACTOR      = 1.0;      // Calibrate with 1000 ppm NaCl solution
float TURB_OFFSET     = 0.0;      // Calibrate with 0 NTU distilled water

// ==================== OBJECTS ====================
DHT dht(DHT_PIN, DHT_TYPE);

// ==================== NOISE FILTER ====================
float readAnalogAvg(int pin, int samples = 30) {
  long sum = 0;
  for (int i = 0; i < samples; i++) {
    sum += analogRead(pin);
    delayMicroseconds(250);
  }
  return (float)sum / samples;
}

// ==================== SETUP ====================
void setup() {
  Serial.begin(115200);   // <- matches Pi SensorGpsLink baud
  dht.begin();

  // Simple one-crlf preamble, ignored by the Pi JSON parser
  Serial.println(F("{\"seq\":0}"));
}

// ==================== LOOP ====================
void loop() {
  static unsigned long seq = 0;
  static unsigned long lastRead = 0;

  // Emit a JSON line every 1 s (Pi staleness timeout is 3 s)
  if (millis() - lastRead < 1000) return;
  lastRead = millis();
  seq++;

  // ---------- DHT11 ----------
  float humidity = dht.readHumidity();
  float tempC    = dht.readTemperature();
  if (isnan(humidity) || isnan(tempC)) { humidity = 0; tempC = 0; }

  // ---------- TDS ----------
  float tdsRaw = readAnalogAvg(TDS_PIN);
  float tdsVoltage = tdsRaw * 5.0 / 1023.0;
  float compFactor = 1.0 + 0.02 * (tempC - 25.0);
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

  // ==================== EMIT JSON FOR RASPBERRY PI ====================
  // Keys must exactly match nav_service SensorGpsLink.sensor_keys.
  // wtemp / atemp both use tempC (single ambient sensor in this sketch);
  // set atemp consistently with your air sensor. Replace the zero GPS fields
  // here if you add a NEO-M8N to a second serial on this board.
  Serial.print(F("{\"seq\":"));
  Serial.print(seq);
  Serial.print(F(",\"gps\":{\"lat\":0.0,\"lon\":0.0,\"alt\":0.0,\"spd\":0.0,\"course\":0.0,\"sats\":0,\"fix\":0}"));
  Serial.print(F(",\"hdg\":0.0"));
  Serial.print(F(",\"sensors\":{"));
  Serial.print(F("\"ph\":"));     Serial.print(phValue, 2);
  Serial.print(F(",\"tds\":"));    Serial.print(tdsValue, 2);
  Serial.print(F(",\"turb\":"));   Serial.print(ntu, 2);
  Serial.print(F(",\"wtemp\":"));  Serial.print(tempC, 2);
  Serial.print(F(",\"atemp\":"));  Serial.print(tempC, 2);
  Serial.print(F(",\"hum\":"));    Serial.print(humidity, 2);
  Serial.print(F("},\"mode\":\"AUTO\""));
  Serial.println(F("}"));
}
