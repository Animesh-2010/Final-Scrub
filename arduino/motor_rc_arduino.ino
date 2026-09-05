/*=================================================================
 SCRUB — Motor + RC Arduino (USB JSON link to Raspberry Pi)

 DC-motor PWM controller for L298N / L9110-style dual H-bridges.

 The Pi sends PWM as differential powers over the blue USB cable
 (HardwareSerial → /dev/ttyUSB0 @ 115200). The Arduino applies real
 PWM on the motor pins and echoes what it received back to the Pi
 so the dashboard can display the ACTUAL PWM at the pins.

   Pi -> Arduino (newline-delimited JSON):
     {"cmd":"motor","l":-255,"r":128}    signed PWM duty (-255..255)
     {"cmd":"ping"}                      heartbeat / no-op

   Arduino -> Pi (one line per reading, ~5 Hz):
     {"seq":1,"mode":"AUTO","rc":{"ch1":1500,"ch2":1500},
      "motor":{"l":-255,"r":128,"pwm_l":-255,"pwm_r":128}}

   l/r > 0 = forward, l/r < 0 = reverse, |l/r|~0 = stop.
   |value| is the raw analogWrite duty (0..255).

 WIRING (Arduino Uno -> L298N):
   Left motor:   PWM A (ENA)  -> D5
                 DIR1 (IN1)   -> D6
                 DIR2 (IN2)   -> D7
   Right motor:  PWM B (ENB)  -> D9
                 DIR1 (IN3)   -> D10
                 DIR2 (IN4)   -> D11
   GND common between Arduino, driver and Pi.

 Requires the ArduinoJson library (v6).
   Sketch > Include Library > Manage Libraries > search "ArduinoJson" > install
=================================================================*/

#include <ArduinoJson.h>

// ── Motor pins (DC motors + L298N / L9110) ────────────────
#define L_PWM    5
#define L_DIR1   6
#define L_DIR2   7

#define R_PWM    9
#define R_DIR1   10
#define R_DIR2   11

// ── Tuning ────────────────────────────────────────────────
#define PWM_DEADBAND  10     // |duty| below this => stop
#define PWM_MIN       15     // lowest non-zero duty (overcome stall)

// ── Commanded motor PWM (set from Pi JSON) ────────────────
volatile int targetL = 0;    // signed PWM -255..255
volatile int targetR = 0;    // signed PWM -255..255

int appliedPwmL = 0;         // last applied signed PWM (-255..255)
int appliedPwmR = 0;         // last applied signed PWM (-255..255)

long seq = 0;

// ──────────────────────────────────────────────────────────
// PWM low-level
// ──────────────────────────────────────────────────────────
// pwm is signed -255..255. Returns the signed duty actually applied
// (0 in the deadband, clamped to ±255, min ±PWM_MIN when non-zero).
int writeMotor(int pwmPin, int dir1, int dir2, int pwm) {
  int duty = pwm;
  if (duty > 255)  duty = 255;
  if (duty < -255) duty = -255;
  int mag = abs(duty);
  if (mag < PWM_DEADBAND) {
    duty = 0;
    mag = 0;
  } else if (mag < PWM_MIN) {
    mag  = PWM_MIN;
    duty = (duty > 0) ? PWM_MIN : -PWM_MIN;
  }

  if (duty > 0) {            // forward
    digitalWrite(dir1, HIGH);
    digitalWrite(dir2, LOW);
  } else if (duty < 0) {     // reverse
    digitalWrite(dir1, LOW);
    digitalWrite(dir2, HIGH);
  } else {                   // stop (coast)
    digitalWrite(dir1, LOW);
    digitalWrite(dir2, LOW);
  }
  analogWrite(pwmPin, mag);

  if (pwmPin == L_PWM) appliedPwmL = duty;
  else                appliedPwmR = duty;
  return duty;
}

void applyMotorCommand() {
  int l, r;
  noInterrupts(); l = targetL; r = targetR; interrupts();
  writeMotor(L_PWM, L_DIR1, L_DIR2, l);
  writeMotor(R_PWM, R_DIR1, R_DIR2, r);
}

// ──────────────────────────────────────────────────────────
// SERIAL: receive Pi command (non-blocking, line-based)
// ──────────────────────────────────────────────────────────
#define LINE_BUF_SZ   128
char lineBuf[LINE_BUF_SZ];
uint8_t lineLen = 0;
bool  lineReady = false;

void readSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (lineLen < LINE_BUF_SZ) lineBuf[lineLen] = '\0';
      lineReady = true;
    } else if (lineLen < LINE_BUF_SZ - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;   // overrun — discard partial line
    }
  }
}

void handleCommand() {
  if (!lineReady) return;
  lineReady = false;
  lineLen = 0;

  StaticJsonDocument<192> doc;
  DeserializationError err = deserializeJson(doc, lineBuf);
  if (err) return;   // ignore malformed heartbeat/echo

  const char* cmd = doc["cmd"] | "";
  if (strcmp(cmd, "motor") == 0) {
    noInterrupts();
    targetL = constrain(doc["l"] | 0, -255, 255);
    targetR = constrain(doc["r"] | 0, -255, 255);
    interrupts();
  } else if (strcmp(cmd, "ping") == 0) {
    // heartbeat — no action
  }
}

// ──────────────────────────────────────────────────────────
// SERIAL: emit telemetry to Pi (~5 Hz)
// ──────────────────────────────────────────────────────────
void emitTelemetry() {
  int l, r;
  noInterrupts(); l = targetL; r = targetR; interrupts();

  StaticJsonDocument<192> out;
  out["seq"]  = ++seq;
  out["mode"] = "AUTO";                       // allows the Pi to command motors
  out["rc"]["ch1"]    = 1500;                 // no RC receiver wired on this build
  out["rc"]["ch2"]    = 1500;
  out["motor"]["l"]   = l;                    // signed PWM received (-255..255)
  out["motor"]["r"]   = r;
  out["motor"]["pwm_l"] = appliedPwmL;        // actual PWM applied at pins (-255..255)
  out["motor"]["pwm_r"] = appliedPwmR;
  serializeJson(out, Serial);
  Serial.println();
}

// ──────────────────────────────────────────────────────────
// SETUP
// ──────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);                 // matches Pi MotorRcLink baud

  pinMode(L_PWM, OUTPUT);  pinMode(L_DIR1, OUTPUT);  pinMode(L_DIR2, OUTPUT);
  pinMode(R_PWM, OUTPUT);  pinMode(R_DIR1, OUTPUT);  pinMode(R_DIR2, OUTPUT);

  // All motors stopped until the Pi commands otherwise
  digitalWrite(L_DIR1, LOW); digitalWrite(L_DIR2, LOW);
  digitalWrite(R_DIR1, LOW); digitalWrite(R_DIR2, LOW);
  analogWrite(L_PWM, 0);
  analogWrite(R_PWM, 0);

  // empty preamble ignored by the Pi parser
  Serial.println(F("{\"seq\":0}"));
}

// ──────────────────────────────────────────────────────────
// LOOP
// ──────────────────────────────────────────────────────────
void loop() {
  readSerial();
  handleCommand();              // receive Pi motor/ping commands (non-blocking)
  applyMotorCommand();          // write PWM + direction per current l/r

  static unsigned long lastEmit = 0;
  if (millis() - lastEmit >= 200) {    // ~5 Hz telemetry (Pi needs >= 1 Hz)
    emitTelemetry();
    lastEmit = millis();
  }
}