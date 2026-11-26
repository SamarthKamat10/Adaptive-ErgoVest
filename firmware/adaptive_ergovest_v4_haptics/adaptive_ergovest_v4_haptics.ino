/*
  ================================================================
  Adaptive ErgoVest - ESP32 Sensor Acquisition Firmware
  ESP32-WROOM-32 (38-pin DevKit)
  ================================================================

  MPU6050:
    SDA -> GPIO13
    SCL -> GPIO14
    I2C address -> 0x68

  Other sensors:
    DHT11 Data -> GPIO27
    DS18B20 Data -> GPIO25
    FSR1/2/3 -> GPIO32/33/34
    Flex1/2/3 -> GPIO35/26/12

  Haptic motors:
    Left motor -> GPIO18
    Right motor -> GPIO19

  Serial commands from Python:
    L = left pulse
    R = right pulse
    B = both motors
    F = fast repeated pulses on both motors
    S = stop motors

  Output:
    16 original sensor fields + forwardTilt + sideTilt + totalTilt
    No upright calibration or baseline is used.
*/

#include <Wire.h>
#include <DHT.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include <math.h>

// ---------------- Pin map ----------------
#define I2C_SDA       13
#define I2C_SCL       14
#define DHT_PIN       27
#define DHT_TYPE      DHT11
#define ONE_WIRE_BUS  25

#define FSR1_PIN      32
#define FSR2_PIN      33
#define FSR3_PIN      34

#define FLEX1_PIN     35
#define FLEX2_PIN     26
#define FLEX3_PIN     12

#define MOTOR_L_PIN   18
#define MOTOR_R_PIN   19

// ---------------- Sensor objects ----------------
DHT dht(DHT_PIN, DHT_TYPE);
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature ds18b20(&oneWire);

DeviceAddress dsAddress;
bool dsFound = false;
bool mpuReady = false;
const uint8_t MPU_ADDR = 0x68;

// MPU6050 register addresses
const uint8_t REG_SMPLRT_DIV   = 0x19;
const uint8_t REG_CONFIG       = 0x1A;
const uint8_t REG_GYRO_CONFIG  = 0x1B;
const uint8_t REG_ACCEL_CONFIG = 0x1C;
const uint8_t REG_ACCEL_XOUT_H = 0x3B;
const uint8_t REG_PWR_MGMT_1   = 0x6B;
const uint8_t REG_WHO_AM_I     = 0x75;

// Write one MPU6050 register using the ESP32 Wire interface.
bool mpuWriteRegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

// Read one MPU6050 register.
bool mpuReadRegister(uint8_t reg, uint8_t &value) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  if (Wire.requestFrom(MPU_ADDR, (uint8_t)1, (uint8_t)true) != 1) {
    return false;
  }

  value = Wire.read();
  return true;
}

// Wake and configure the MPU6050 directly, without the Adafruit library.
bool initializeMPU6050() {
  uint8_t whoAmI = 0;

  if (!mpuReadRegister(REG_WHO_AM_I, whoAmI)) {
    Serial.println("MPU6050: WHO_AM_I register read failed.");
    return false;
  }

  Serial.print("MPU6050 WHO_AM_I = 0x");
  Serial.println(whoAmI, HEX);

  // Genuine MPU6050 normally reports 0x68. Some compatible modules
  // may report a different identity, so verify I2C access and configure.
  if (!mpuWriteRegister(REG_PWR_MGMT_1, 0x00)) return false; // wake, use internal clock
  delay(100);

  if (!mpuWriteRegister(REG_CONFIG, 0x04)) return false;       // ~21 Hz DLPF
  if (!mpuWriteRegister(REG_SMPLRT_DIV, 19)) return false;     // 50 Hz output rate
  if (!mpuWriteRegister(REG_GYRO_CONFIG, 0x08)) return false;   // +/-500 deg/s
  if (!mpuWriteRegister(REG_ACCEL_CONFIG, 0x10)) return false; // +/-8 g

  delay(50);
  return true;
}

// Read all accelerometer and gyroscope axes in one 14-byte transaction.
// Acceleration is returned in m/s^2; gyro is returned in rad/s,
// matching the units previously produced by Adafruit_MPU6050.
bool readMPU6050(float &ax, float &ay, float &az,
                 float &gx, float &gy, float &gz) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT_H);

  if (Wire.endTransmission(false) != 0) {
    return false;
  }

  uint8_t received = Wire.requestFrom(MPU_ADDR, (uint8_t)14, (uint8_t)true);
  if (received != 14) {
    while (Wire.available()) Wire.read();
    return false;
  }

  int16_t rawAx = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawAy = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawAz = (int16_t)((Wire.read() << 8) | Wire.read());

  // Temperature bytes are present between accelerometer and gyro data.
  Wire.read();
  Wire.read();

  int16_t rawGx = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawGy = (int16_t)((Wire.read() << 8) | Wire.read());
  int16_t rawGz = (int16_t)((Wire.read() << 8) | Wire.read());

  const float ACCEL_SCALE = 4096.0f; // LSB/g at +/-8 g
  const float GYRO_SCALE = 65.5f;    // LSB/(deg/s) at +/-500 deg/s
  const float GRAVITY = 9.80665f;

  ax = ((float)rawAx / ACCEL_SCALE) * GRAVITY;
  ay = ((float)rawAy / ACCEL_SCALE) * GRAVITY;
  az = ((float)rawAz / ACCEL_SCALE) * GRAVITY;

  gx = ((float)rawGx / GYRO_SCALE) * (PI / 180.0f);
  gy = ((float)rawGy / GYRO_SCALE) * (PI / 180.0f);
  gz = ((float)rawGz / GYRO_SCALE) * (PI / 180.0f);

  return true;
}

// ---------------- Haptics ----------------
struct Pulse {
  bool left;
  bool right;
  int remaining;
  unsigned long onMs;
  unsigned long offMs;
  unsigned long nextChange;
  bool on;
};

Pulse pulse = {false, false, 0, 0, 0, 0, false};

void motors(bool left, bool right) {
  digitalWrite(MOTOR_L_PIN, left ? HIGH : LOW);
  digitalWrite(MOTOR_R_PIN, right ? HIGH : LOW);
}

void startPulse(bool left, bool right, int count,
                unsigned long onMs, unsigned long offMs) {
  pulse = {left, right, count, onMs, offMs, millis() + onMs, true};
  motors(left, right);
}

void updateHaptics() {
  if (pulse.remaining <= 0 || millis() < pulse.nextChange) {
    return;
  }

  if (pulse.on) {
    motors(false, false);
    pulse.on = false;
    pulse.remaining--;
    pulse.nextChange = millis() + pulse.offMs;
  } else if (pulse.remaining > 0) {
    motors(pulse.left, pulse.right);
    pulse.on = true;
    pulse.nextChange = millis() + pulse.onMs;
  }
}

void handleSerialCommands() {
  while (Serial.available() > 0) {
    char command = Serial.read();

    if (command == 'L') {
      startPulse(true, false, 2, 200, 150);
    } else if (command == 'R') {
      startPulse(false, true, 2, 200, 150);
    } else if (command == 'B') {
      startPulse(true, true, 2, 250, 150);
    } else if (command == 'F') {
      startPulse(true, true, 5, 100, 80);
    } else if (command == 'S') {
      pulse.remaining = 0;
      pulse.on = false;
      motors(false, false);
    }
  }
}

// ---------------- Timing ----------------
const unsigned long FAST_INTERVAL_MS = 20;       // 50 Hz
const unsigned long SLOW_INTERVAL_MS = 2000;     // 2 seconds
const unsigned long DS18B20_CONVERSION_MS = 750; // 12-bit conversion

unsigned long lastFastSample = 0;
unsigned long lastSlowTrigger = 0;
unsigned long ds18b20RequestTime = 0;

bool ds18b20ConversionPending = false;

// ---------------- Cached slow-sensor values ----------------
float cachedHumidity = NAN;
float cachedAmbientTempC = NAN;
float cachedDsTempC = NAN;

// ---------------- Setup ----------------
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(MOTOR_L_PIN, OUTPUT);
  pinMode(MOTOR_R_PIN, OUTPUT);
  motors(false, false);

  // Initialize I2C on the pins used by this project.
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  // Initialize the MPU6050 directly over I2C.
  // The I2C scanner already confirmed that address 0x68 responds.
  mpuReady = initializeMPU6050();

  if (mpuReady) {
    Serial.println("MPU6050 ready (direct I2C register mode).");
  } else {
    Serial.println("ERROR: MPU6050 direct initialization failed.");
    Serial.println("The remaining sensors will continue to be printed.");
  }

  dht.begin();

  ds18b20.begin();
  ds18b20.setWaitForConversion(false);

  int deviceCount = ds18b20.getDeviceCount();
  Serial.printf("DS18B20 devices found on bus: %d\n", deviceCount);

  dsFound = ds18b20.getAddress(dsAddress, 0);
  if (!dsFound) {
    Serial.println("ERROR: DS18B20 not found.");
  } else {
    ds18b20.setResolution(dsAddress, 12);
  }

  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  Serial.println("Adaptive ErgoVest sensor node initialized.");
  Serial.println(
    "timestamp_ms,ax,ay,az,gx,gy,gz,humidity,ambientTempC,dsTempC,"
    "fsr1,fsr2,fsr3,flex1,flex2,flex3,forwardTilt,sideTilt,totalTilt"
  );
}

// ---------------- Main loop ----------------
void loop() {
  unsigned long now = millis();

  handleSerialCommands();
  updateHaptics();

  // Read DHT11 and request DS18B20 conversion every 2 seconds.
  if (!ds18b20ConversionPending &&
      (now - lastSlowTrigger >= SLOW_INTERVAL_MS)) {

    lastSlowTrigger = now;

    cachedHumidity = dht.readHumidity();
    cachedAmbientTempC = dht.readTemperature();

    if (dsFound) {
      ds18b20.requestTemperatures();
      ds18b20RequestTime = now;
      ds18b20ConversionPending = true;
    }
  }

  // Collect the DS18B20 result after its conversion period.
  if (ds18b20ConversionPending &&
      (now - ds18b20RequestTime >= DS18B20_CONVERSION_MS)) {

    if (dsFound) {
      cachedDsTempC = ds18b20.getTempC(dsAddress);
    }

    ds18b20ConversionPending = false;
  }

  // Sample IMU, FSRs and flex sensors at 50 Hz.
  if (now - lastFastSample >= FAST_INTERVAL_MS) {
    lastFastSample = now;
    sampleAndPrintFast();
  }
}

// ---------------- Read and print all sensors ----------------
void sampleAndPrintFast() {
  float ax = NAN;
  float ay = NAN;
  float az = NAN;

  float gx = NAN;
  float gy = NAN;
  float gz = NAN;

  float forwardTilt = NAN;
  float sideTilt = NAN;
  float totalTilt = NAN;

  // Read MPU6050 registers directly. No Adafruit mpu.begin() call.
  if (mpuReady && readMPU6050(ax, ay, az, gx, gy, gz)) {
    /*
      Direct angle calculations from acceleration.

      Based on the current readings, Y is treated as the
      approximate vertical axis. These are gravity-referenced
      angles, not deviations from a calibrated upright posture.
    */
        // MPU6050 orientation:
    // Y-axis = vertical when upright
    // Z-axis = forward bending direction

    forwardTilt = atan2f(
      fabsf(az),
      fabsf(ay)
    ) * 180.0f / PI;

    // Side tilt
    sideTilt = atan2f(
      fabsf(ax),
      sqrtf(ay * ay + az * az)
    ) * 180.0f / PI;

    // Total deviation from upright
    totalTilt = atan2f(
      sqrtf(ax * ax + az * az),
      fabsf(ay)
    ) * 180.0f / PI;
  } else {
    // A failed read is marked invalid; retry initialization on the next sample.
    mpuReady = false;
  }

  if (!mpuReady) {
    static unsigned long lastMpuRetry = 0;
    if (millis() - lastMpuRetry >= 1000) {
      lastMpuRetry = millis();
      mpuReady = initializeMPU6050();
      if (mpuReady) {
        Serial.println("MPU6050 communication restored.");
      }
    }
  }

  // Read pressure and flex sensors.
  int fsr1 = analogRead(FSR1_PIN);
  int fsr2 = analogRead(FSR2_PIN);
  int fsr3 = analogRead(FSR3_PIN);

  int flex1 = analogRead(FLEX1_PIN);
  int flex2 = analogRead(FLEX2_PIN);
  int flex3 = analogRead(FLEX3_PIN);

  // Print exactly 19 comma-separated fields for the Python parser.
  Serial.print(millis());
  Serial.print(",");

  Serial.print(ax, 3);
  Serial.print(",");
  Serial.print(ay, 3);
  Serial.print(",");
  Serial.print(az, 3);
  Serial.print(",");

  Serial.print(gx, 4);
  Serial.print(",");
  Serial.print(gy, 4);
  Serial.print(",");
  Serial.print(gz, 4);
  Serial.print(",");

  Serial.print(cachedHumidity, 2);
  Serial.print(",");
  Serial.print(cachedAmbientTempC, 2);
  Serial.print(",");
  Serial.print(cachedDsTempC, 2);
  Serial.print(",");

  Serial.print(fsr1);
  Serial.print(",");
  Serial.print(fsr2);
  Serial.print(",");
  Serial.print(fsr3);
  Serial.print(",");

  Serial.print(flex1);
  Serial.print(",");
  Serial.print(flex2);
  Serial.print(",");
  Serial.print(flex3);
  Serial.print(",");

  Serial.print(forwardTilt, 2);
  Serial.print(",");
  Serial.print(sideTilt, 2);
  Serial.print(",");
  Serial.println(totalTilt, 2);
}
