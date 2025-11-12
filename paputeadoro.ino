#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <SPI.h>
#include <SD.h>
#include "driver/i2s.h"

/************ WiFi / Server ************/
const char* WIFI_SSID = "Wifi_Ucentral";
const char* WIFI_PASS = "UCEN2009";
const char* SERVER_HOST = "10.58.9.116";  // IP del PC
const int   SERVER_PORT = 8000;
const char* URL_PTT     = "/api/ptt";

/************ Botón ************/
static const int PIN_BUTTON = 13;

/************ SD - VSPI ************/
static const int PIN_SD_CS   = 5;
static const int PIN_SD_SCK  = 18;
static const int PIN_SD_MISO = 19;
static const int PIN_SD_MOSI = 23;

/************ I2S IN (INMP441) ************/
static const i2s_port_t I2S_IN_PORT = I2S_NUM_0;
static const int PIN_I2S_IN_BCLK = 25;  // SCK/BCLK
static const int PIN_I2S_IN_WS   = 26;  // LRCLK/WS
static const int PIN_I2S_IN_DIN  = 35;  // SD/DOUT mic
static const bool I2S_IN_IS_LEFT = true; // L/R a GND => canal izquierdo
static const uint32_t I2S_IN_FS  = 16000;

/************ I2S OUT (MAX98357A) ************/
static const i2s_port_t I2S_OUT_PORT = I2S_NUM_1;
static const int PIN_I2S_OUT_BCLK = 27; // BCLK
static const int PIN_I2S_OUT_WS   = 14; // LRCLK
static const int PIN_I2S_OUT_DOUT = 33; // DIN

/************ WAV ************/
#pragma pack(push,1)
struct WAVHeader {
  char     riffId[4]; uint32_t riffSize; char waveId[4];
  char     fmtId[4];  uint32_t fmtSize;  uint16_t audioFormat;
  uint16_t numChannels; uint32_t sampleRate; uint32_t byteRate;
  uint16_t blockAlign; uint16_t bitsPerSample;
  char     dataId[4]; uint32_t dataSize;
};
#pragma pack(pop)

static const uint16_t PCM_BITS = 16;
static const size_t   CHUNK_SAMPLES = 1024;

File     f_rec;
uint32_t dataBytes = 0;

/************ Util WAV ************/
void wav_write_header(File &f, uint32_t sampleRate, uint16_t bits, uint16_t ch) {
  WAVHeader h;
  memcpy(h.riffId, "RIFF", 4);  h.riffSize = 36;
  memcpy(h.waveId, "WAVE", 4);
  memcpy(h.fmtId,  "fmt ", 4);  h.fmtSize = 16; h.audioFormat = 1;
  h.numChannels   = ch;
  h.sampleRate    = sampleRate;
  h.bitsPerSample = bits;
  h.blockAlign    = (ch * bits) / 8;
  h.byteRate      = sampleRate * h.blockAlign;
  memcpy(h.dataId, "data", 4);  h.dataSize = 0;
  f.write((const uint8_t*)&h, sizeof(h));
}
void wav_finalize(File &f, uint32_t bytes) {
  f.seek(40); f.write((uint8_t*)&bytes, 4);
  uint32_t riffSize = 36 + bytes; f.seek(4); f.write((uint8_t*)&riffSize, 4);
  f.flush();
}

/************ SD ************/
bool sd_begin(uint32_t hz=4000000) {
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  if (!SD.begin(PIN_SD_CS, SPI, hz)) { Serial.println("[SD] begin FAIL"); return false; }
  Serial.printf("[SD] type=%d size=%lluMB\n", SD.cardType(), SD.cardSize()/1024ULL/1024ULL);
  return true;
}

/************ I2S IN ************/
bool i2s_in_begin() {
  i2s_config_t cfg{};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = I2S_IN_FS;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT; // INMP441 entrega 24b alineado a 32b
  cfg.channel_format = I2S_IN_IS_LEFT ? I2S_CHANNEL_FMT_ONLY_LEFT : I2S_CHANNEL_FMT_ONLY_RIGHT;
  cfg.communication_format = I2S_COMM_FORMAT_I2S; // si oyes "viento", prueba I2S_COMM_FORMAT_STAND_MSB
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = false;
  cfg.fixed_mclk = 0;

  i2s_pin_config_t p{};
  p.bck_io_num   = PIN_I2S_IN_BCLK;
  p.ws_io_num    = PIN_I2S_IN_WS;
  p.data_out_num = I2S_PIN_NO_CHANGE;
  p.data_in_num  = PIN_I2S_IN_DIN;
#if ESP_IDF_VERSION_MAJOR >= 4
  p.mck_io_num   = I2S_PIN_NO_CHANGE; // deshabilita MCLK
#endif

  esp_err_t e = i2s_driver_install(I2S_IN_PORT, &cfg, 0, NULL);
  if (e != ESP_OK) { Serial.printf("[I2S IN] install FAIL %s\n", esp_err_to_name(e)); return false; }
  e = i2s_set_pin(I2S_IN_PORT, &p);
  if (e != ESP_OK) { Serial.printf("[I2S IN] set_pin FAIL %s\n", esp_err_to_name(e)); return false; }
  e = i2s_set_clk(I2S_IN_PORT, I2S_IN_FS, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_MONO);
  if (e != ESP_OK) { Serial.printf("[I2S IN] set_clk WARN %s\n", esp_err_to_name(e)); }
  i2s_zero_dma_buffer(I2S_IN_PORT);
  Serial.printf("[I2S IN] Fs=%u, fmt=I2S, ch=%s\n", I2S_IN_FS, I2S_IN_IS_LEFT?"L":"R");
  return true;
}

/************ I2S OUT ************/
bool i2s_out_begin(uint32_t fs) {
  static bool installed=false;
  if (installed) {
    i2s_driver_uninstall(I2S_OUT_PORT);
    installed=false;
  }

  i2s_config_t cfg{};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = fs;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT; // monofónico
  cfg.communication_format = I2S_COMM_FORMAT_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 8;
  cfg.dma_buf_len = 256;  // si chasquea, prueba 512
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;
  cfg.fixed_mclk = 0;

  i2s_pin_config_t p{};
  p.bck_io_num   = PIN_I2S_OUT_BCLK;
  p.ws_io_num    = PIN_I2S_OUT_WS;
  p.data_out_num = PIN_I2S_OUT_DOUT;
  p.data_in_num  = I2S_PIN_NO_CHANGE;
#if ESP_IDF_VERSION_MAJOR >= 4
  p.mck_io_num   = I2S_PIN_NO_CHANGE; // deshabilita MCLK (MAX98357A no lo usa)
#endif

  esp_err_t e = i2s_driver_install(I2S_OUT_PORT, &cfg, 0, NULL);
  if (e != ESP_OK) { Serial.println("[I2S OUT] install FAIL"); return false; }
  e = i2s_set_pin(I2S_OUT_PORT, &p);
  if (e != ESP_OK) { Serial.println("[I2S OUT] set_pin FAIL"); return false; }
  e = i2s_set_clk(I2S_OUT_PORT, fs, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
  if (e != ESP_OK) { Serial.println("[I2S OUT] set_clk WARN"); }
  i2s_zero_dma_buffer(I2S_OUT_PORT);
  installed = true;
  Serial.printf("[I2S OUT] Fs=%u\n", fs);
  return true;
}

/************ Grabación a WAV en SD ************/
bool rec_start(const char* path) {
  f_rec = SD.open(path, FILE_WRITE);
  if (!f_rec) return false;
  wav_write_header(f_rec, I2S_IN_FS, 16, 1);
  dataBytes = 0;
  i2s_zero_dma_buffer(I2S_IN_PORT);
  Serial.printf("[REC] Grabando a %s (mantén pulsado el botón)...\n", path);
  return true;
}
void rec_append() {
  static int32_t in32[CHUNK_SAMPLES];
  static int16_t out16[CHUNK_SAMPLES];
  size_t br=0;
  if (i2s_read(I2S_IN_PORT, (void*)in32, sizeof(in32), &br, portMAX_DELAY) != ESP_OK) return;
  int n = br / sizeof(int32_t);
  for (int i=0;i<n;i++){
    int32_t v24 = (in32[i] >> 8) & 0x00FFFFFF; if (v24 & 0x00800000) v24 |= 0xFF000000;
    int16_t s16 = (int16_t)(v24 >> 8); // 24->16
    out16[i] = s16;
  }
  size_t bw = f_rec.write((uint8_t*)out16, n * sizeof(int16_t));
  dataBytes += bw;
}
void rec_stop() {
  wav_finalize(f_rec, dataBytes);
  f_rec.close();
  Serial.printf("[REC] Listo. %u bytes PCM\n", dataBytes);
}

/************ POST + Streaming playback ************/
bool post_and_stream_play(const char* inPath) {
  File fin = SD.open(inPath, FILE_READ);
  if (!fin) { Serial.println("[HTTP] no se pudo abrir WAV"); return false; }

  WiFiClient client;
  client.setTimeout(60000);
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + URL_PTT;

  if (!http.begin(client, url)) { fin.close(); return false; }
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("Connection", "close");
  http.setTimeout(60000);

  Serial.printf("[HTTP] POST %s (%u bytes)\n", url.c_str(), (unsigned)fin.size());
  int code = http.sendRequest("POST", &fin, fin.size());
  fin.close();
  if (code != 200) {
    Serial.printf("[HTTP] code=%d\n", code);
    http.end(); return false;
  }

  WiFiClient* s = http.getStreamPtr();

  // Lee cabecera WAV de la respuesta
  uint8_t hdr[44];
  int got = s->readBytes(hdr, 44);
  if (got != 44 || memcmp(hdr, "RIFF", 4)!=0 || memcmp(hdr+8, "WAVE", 4)!=0) {
    Serial.println("[PLAY] WAV inválido en respuesta");
    http.end(); return false;
  }
  uint32_t sr   = *(uint32_t*)(hdr+24);
  uint16_t bits = *(uint16_t*)(hdr+34);
  if (bits != 16) { Serial.println("[PLAY] bits != 16"); http.end(); return false; }

  // Arranca salida con Fs del WAV recibido
  if (!i2s_out_begin(sr)) { http.end(); return false; }

  Serial.println("[STREAM] Reproduciendo en streaming...");
  static uint8_t buf[1024];
  uint32_t t0 = millis(), last = t0;
  while (http.connected()) {
    int a = s->available();
    if (a > 0) {
      int r = s->readBytes(buf, a > (int)sizeof(buf) ? (int)sizeof(buf) : a);
      if (r <= 0) break;
      size_t b;
      // buf es PCM 16-bit (little-endian) mono
      i2s_write(I2S_OUT_PORT, (const char*)buf, r, &b, portMAX_DELAY);
      last = millis();
    } else {
      if (millis()-last > 60000) { Serial.println("[STREAM] timeout"); break; }
      delay(1);
    }
  }
  i2s_zero_dma_buffer(I2S_OUT_PORT);
  http.end();
  return true;
}

/************ Helpers ************/
bool waitLevel(int lvl, uint32_t ms) {
  uint32_t t0 = millis();
  while (millis()-t0 < ms) { if (digitalRead(PIN_BUTTON)==lvl) return true; delay(5); }
  return false;
}

/************ Setup/Loop ************/
void setup() {
  Serial.begin(115200); delay(300);
  Serial.println("\n=== ESP32: Mic->SD->POST->StreamPlay ===");

  pinMode(PIN_BUTTON, INPUT_PULLUP);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("[WiFi] Conectando a %s ...\n", WIFI_SSID);
  while (WiFi.status()!=WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.printf("\n[WiFi] OK %s  IP=%s\n", WIFI_SSID, WiFi.localIP().toString().c_str());

  if (!sd_begin()) Serial.println("[SD] FAIL");
  if (!i2s_in_begin()) Serial.println("[I2S IN] FAIL");

  Serial.println("[READY] Mantén presionado para GRABAR; al soltar: envía y reproduce.");
}

void loop() {
  if (digitalRead(PIN_BUTTON)==LOW) {
    if (!rec_start("/ptt.wav")) { delay(500); return; }
    while (digitalRead(PIN_BUTTON)==LOW) rec_append();
    rec_stop();

    if (!post_and_stream_play("/ptt.wav")) {
      Serial.println("[MAIN] POST/stream falló");
    }
  }
  delay(5);
}
