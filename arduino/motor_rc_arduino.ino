/*=================================================================
 SCRUB — Motor + RC Arduino (USB JSON link to Raspberry Pi)

 Speaks the nav_service MotorRcLink wire protocol on HardwareSerial
 (the Pi listens on USB -> /dev/ttyACM0 @ 115200).

   Pi -> Arduino (newline-delimited JSON):
     {"cmd":"motor","l":18,"r":22}     differential motor power (-100..100)
     {"cmd":"ping"}                    heartbeat / no-op

   Arduino -> Pi (one line per reading, ~5 Hz):
     {"seq":1,"mode":"AUTO","rc":{"ch1":1500,"ch2":1500}}

 The Pi only drives the motors when effective mode is AUTO. This sketch
 reports mode:"AUTO" by default so the Pi can command it. RC channel
 values are still read (interrupt-based) and reported for telemetry.

 Requires the ArduinoJson library.
   Sketch > Include Library > Manage Libraries > search "ArduinoJson" > install
=================================================================*/

#include <ArduinoJson.h>

// ── Stepper pins ─────────────────────────────────────
#define L_PUL   8
#define L_DIR   9
#define L_EN    4

#define R_PUL   10
#define R_DIR   11
#define R_EN    7

// ── RC pins (must be INT0=D2 and INT1=D3 on Uno) ────
#define CH1_PIN   2    // INT0 — steering
#define CH3_PIN   3    // INT1 — throttle

// ── Tuning ───────────────────────────────────────────
#define STEP_DELAY_US   700    // base step period (lower = faster)
#define STEP_BATCH      10     // steps per command cycle
#define MOTOR_DEADBAND  10     // |power| below this => stop

// ── RC pulse storage (written by ISR, read by loop) ──
volatile unsigned long ch1RiseTime = 0;
volatile unsigned long ch3RiseTime = 0;
volatile int ch1Pulse = 1500;   // raw pulse width us (1000-2000)
volatile int ch3Pulse = 1500;

// ── Commanded motor powers (set from Pi JSON) ────────
volatile int targetL = 0;   // -100..100
volatile int targetR = 0;   // -100..100

long seq = 0;

// ─────────────────────────────────────────────────────
// INTERRUPTS — RC pulse measurement
// ─────────────────────────────────────────────────────
void isr_CH1() {
  if (digitalRead(CH1_PIN) == HIGH) {
    ch1RiseTime = micros();
  } else {
    unsigned long pw = micros() - ch1RiseTime;
    if (pw > 800 && pw < 2200) ch1Pulse = (int)pw;
  }
}

void isr_CH3() {
  if (digitalRead(CH3_PIN) == HIGH) {
    ch3RiseTime = micros();
  } else {
    unsigned long pw = micros() - ch3RiseTime;
    if (pw > 800 && pw < 2200) ch3Pulse = (int)pw;
  }
}

// ─────────────────────────────────────────────────────
// STEPPER LOW-LEVEL
// ─────────────────────────────────────────────────────
void leftStep() { digitalWrite(L_PUL, HIGH); delayMicroseconds(5); digitalWrite(L_PUL, LOW); delayMicroseconds(STEP_DELAY_US); }
void rightStep(){ digitalWrite(R_PUL, HIGH); delayMicroseconds(5); digitalWrite(R_PUL, LOW); delayMicroseconds(STEP_DELAY_US); }

// Drive one side by signed power (-100..100).
//   power > 0 : dir forward, power steps
//   power < 0 : dir backward, |power| steps
//   power ~0  : no motion
void driveLeft(int power) {
  if (abs(power) < MOTOR_DEADBAND) return;
  digitalWrite(L_DIR, (power > 0) ? HIGH : LOW);
  int n = map(abs(power), 0, 100, 1, STEP_BATCH);
  for (int i = 0; i < n; i++) leftStep();
}

void driveRight(int power) {
  if (abs(power) < MOTOR_DEADBAND) return;
  digitalWrite(R_DIR, (power > 0) ? HIGH : LOW);
  int n = map(abs(power), 0, 100, 1, STEP_BATCH);
  for (int i = 0; i < n; i++) rightStep();
}

// Center the differential command: steer by left-right power difference.
void applyMotorCommand() {
  int l, r;
  noInterrupts(); l = targetL; r = targetR; interrupts();
  driveLeft(l);
  driveRight(r);
}

// ─────────────────────────────────────────────────────
// SERIAL: receive Pi command (non-blocking)
// ─────────────────────────────────────────────────────
StaticJsonDocument<128> inbound;

void handleSerial() {
  while (Serial.available()) {
    StaticJsonDocument<128> doc;
    DeserializationError err = deserializeJson(doc, Serial);
    if (err) { // skip partial/garbage and keep waiting for a full line
      while (Serial.available() && Serial.peek() != '\n') Serial.read();
      continue;
    }
    const char* cmd = doc["cmd"] | "";
    if (strcmp(cmd, "motor") == 0) {
      noInterrupts();
      targetL = constrain((int)doc["l"], -100, 100);
      targetR = constrain((int)doc["r"], -100, 100);
      interrupts();
    } else if (strcmp(cmd, "ping") == 0) {
      // heartbeat — respond on next telemetry emit, no action
    }
  }
}

// ─────────────────────────────────────────────────────
// SERIAL: emit telemetry to Pi (~5 Hz)
// ─────────────────────────────────────────────────────
void emitTelemetry() {
  int rawCH1, rawCH3;
  noInterrupts(); rawCH1 = ch1Pulse; rawCH3 = ch3Pulse; interrupts();

  StaticJsonDocument<192> out;
  out["seq"]  = ++seq;
  out["mode"] = "AUTO";                 // allows the Pi to command motors
  out["rc"]["ch1"] = rawCH1;            // raw us 1000-2000
  out["rc"]["ch2"] = rawCH3;
  serializeJson(out, Serial);
  Serial.println();
}

// ─────────────────────────────────────────────────────
// SETUP
// ─────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);                 // <- matches Pi MotorRcLink baud

  pinMode(L_PUL, OUTPUT); pinMode(L_DIR, OUTPUT); pinMode(L_EN, OUTPUT);
  pinMode(R_PUL, OUTPUT); pinMode(R_DIR, OUTPUT); pinMode(R_EN, OUTPUT);

  digitalWrite(L_EN, LOW);              // LOW = enabled on TB6600
  digitalWrite(R_EN, LOW);

  pinMode(CH1_PIN, INPUT);
  pinMode(CH3_PIN, INPUT);

  attachInterrupt(digitalPinToInterrupt(CH1_PIN), isr_CH1, CHANGE);
  attachInterrupt(digitalPinToInterrupt(CH3_PIN), isr_CH3, CHANGE);

  // empty preamble ignored by the Pi parser
  Serial.println(F("{\"seq\":0}"));
}

// ─────────────────────────────────────────────────────
// LOOP
// ─────────────────────────────────────────────────────
void loop() {
  handleSerial();               // receive Pi motor/ping commands (non-blocking)
  applyMotorCommand();          // step the steppers per current l/r
  static unsigned long lastEmit = 0;
  if (millis() - lastEmit >= 200) {    // ~5 Hz telemetry (Pi likes >= ~1 Hz)
    emitTelemetry();
    lastEmit = millis();
  }
}
