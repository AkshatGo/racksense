/*
 * RackSense ESP32 rack controller
 * ------------------------------------------------------------------
 * Hardware assumed
 *   4 x 50 kg strain-gauge load cells wired in parallel (one corner each)
 *   1 x HX711 24-bit ADC  (DT -> GPIO 4, SCK -> GPIO 5)
 *   1 x active buzzer     (GPIO 25)
 *   1 x 0.96" SSD1306 OLED, I2C (SDA 21, SCL 22) — optional
 *   4 x 18650 cells + TP4056 + 5 V boost, as in the project power budget
 *
 * Behaviour
 *   - connects to Wi-Fi and posts the stabilised rack weight every 500 ms
 *   - server answers {"buzz":true,...} the moment an armed rack is breached
 *   - polls the command queue (buzz / tare) for anything queued while offline
 *   - sends the empty-scale reading once at boot so tare is hardware-exact
 *
 * Set the three constants below, upload, and the rack shows up on the
 * dashboard under its own device key.
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <HX711.h>

// ----------------------------- configuration -----------------------------
const char* WIFI_SSID   = "YOUR_WIFI";
const char* WIFI_PASS   = "YOUR_PASSWORD";
const char* SERVER      = "http://192.168.1.10:8060";   // RackSense host
const char* DEVICE_KEY  = "rk_xxxxxxxxxxxxxxxx";         // from the Devices tab

const int   PIN_DT      = 4;
const int   PIN_SCK     = 5;
const int   PIN_BUZZER  = 25;

const float CALIBRATION = -21.3f;   // counts per gram, measured with a known mass
const int   SAMPLES     = 5;        // median window per report
// -------------------------------------------------------------------------

HX711 scale;
unsigned long lastReport = 0;
unsigned long lastPoll = 0;

float readGrams() {
  float v[SAMPLES];
  for (int i = 0; i < SAMPLES; i++) v[i] = scale.get_units(1) * 1000.0f;
  // median of the window ignores a hand brushing the shelf
  for (int i = 0; i < SAMPLES - 1; i++)
    for (int j = i + 1; j < SAMPLES; j++)
      if (v[j] < v[i]) { float t = v[i]; v[i] = v[j]; v[j] = t; }
  return v[SAMPLES / 2];
}

void buzz(int pattern) {
  for (int i = 0; i < 3 * pattern; i++) {
    digitalWrite(PIN_BUZZER, HIGH); delay(140);
    digitalWrite(PIN_BUZZER, LOW);  delay(90);
  }
}

bool postJson(const String& path, const String& body) {
  HTTPClient http;
  http.begin(String(SERVER) + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code = http.POST(body);
  String response = http.getString();
  http.end();

  if (code >= 400) {
    Serial.printf("POST %s -> %d\n", path.c_str(), code);
    return false;
  }
  if (response.indexOf("\"buzz\":true") >= 0) {
    buzz(2);
  }
  return true;
}

void pollCommands() {
  HTTPClient http;
  http.begin(String(SERVER) + "/api/device/" + DEVICE_KEY + "/commands");
  if (http.GET() != 200) { http.end(); return; }
  String payload = http.getString();
  http.end();

  JsonDocument doc;
  if (deserializeJson(doc, payload) != DeserializationError::Ok) return;
  for (JsonObject cmd : doc["commands"].as<JsonArray>()) {
    String name = cmd["cmd"].as<String>();
    if (name == "buzz")  buzz(cmd["payload"]["pattern"] | 1);
    if (name == "tare")  scale.tare(30);
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

  scale.begin(PIN_DT, PIN_SCK);
  scale.set_scale(CALIBRATION);
  delay(400);
  scale.tare(30);                       // empty shelf at power-on (deck: tare & baseline)

  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(400); Serial.print('.'); }
  Serial.printf("\nRackSense online: %s\n", WiFi.localIP().toString().c_str());

  // hardware-exact tare so the shop owner never guesses the crate weight
  postJson(String("/api/device/") + DEVICE_KEY + "/tare",
           "{\"weight_g\":" + String(readGrams(), 0) + "}");
}

void loop() {
  unsigned long now = millis();

  if (now - lastReport >= 500) {
    lastReport = now;
    float grams = readGrams();
    String body = "{\"weight_g\":" + String(grams, 0) + "}";
    if (WiFi.status() == WL_CONNECTED) postJson("/api/device/telemetry", body);
    else WiFi.reconnect();
    Serial.printf("%.0f g\n", grams);
  }

  if (now - lastPoll >= 3000) {           // buzzer / tare queue
    lastPoll = now;
    if (WiFi.status() == WL_CONNECTED) pollCommands();
  }
}
