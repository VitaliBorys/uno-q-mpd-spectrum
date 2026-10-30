// MPD controller + spectrum analyzer - MCU side.
//
// 1. LED matrix: draws 104-byte frames from Python (the 13-band analyzer).
// 2. 2.4" ST7789 TFT (320x240) + EC11 knob + K0 button: a physical remote
//    for MPD. Python sends what is playing, the volume, the clock and the
//    audio levels; the sketch draws the screens and sends the knob and
//    button actions back.
//
// Controls
//   Views (Now playing / Spectrum / VU meters / Magic eye):
//     turn            volume
//     press + turn    switch view
//     short press     play / pause
//     hold 1 s        next track, then one more every second while held
//     K0              open the menu
//   Menu:
//     turn            move
//     press + turn    jump a page
//     short press     open / play (an album or station replaces the queue)
//     hold 1 s        add to the end of the queue
//     K0              back (from the top level: back to the view)
//   After a minute without playback the screen shows the "digital rain".
//
// Messages (Router Bridge)
//   Python -> MCU: draw(frame) trk(...) st(...) lv(levels) clk(minutes)
//                  page(list, ctx, start, total) item(list, ctx, index, text)
//   MCU -> Python: vol(clicks) cmd(code) hello()
//                  get(list, ctx, start) sel(list, ctx, index, action)
// Menu lists live in Python; the sketch asks for one page (8 rows) at a time.
// Bridge callbacks run in the Bridge thread, not in loop(): they only copy
// the data into an inbox, and loop() draws it. Two threads must never
// talk to the display at the same time.
//
// Wiring (module -> UNO Q):
//   GND -> GND   VCC -> 3.3V   SCL -> D13   SDA -> D11
//   RES -> D9    DC -> D8      CS -> D10    BLK -> D6
//   A -> D2      B -> D3       PUSH -> D4   K0 -> D5

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>
#include <DIYables_TFT_SPI.h>
#include <math.h>
#include <string.h>
#include <ctype.h>
#include "font5x7.h"
#include "font5x7_cyr.h"
#include "font5x7_kana.h"

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#ifndef digitalPinToInterrupt
#define digitalPinToInterrupt(p) (p)
#endif

#define TFT_CS   10
#define TFT_DC    8
#define TFT_RST   9
#define TFT_BLK   6
#define PIN_A     2
#define PIN_B     3
#define PIN_PUSH  4
#define PIN_K0    5

// ---- Settings ----------------------------------------------------------
const int STEPS_PER_DETENT = 4;
const bool REVERSE = false;
const unsigned long HOLD_MS = 1000;    // hold this long: next track (fires while held)
const unsigned long REPEAT_MS = 1000;  // ...and again every this long
const unsigned long DEBOUNCE_MS = 25;
const unsigned long FRAME_MS = 33;     // animation: ~30 fps
const bool BUTTON_IRQ = true;          // false = poll buttons only
const bool SHOW_MS = true;             // redraw time in the corner
const bool SPI16 = true;               // 16-bit SPI words for pixels (faster)
const unsigned long IDLE_MS = 60000;   // no playback this long -> digital rain
const int VOLUME_STEP = 2;             // percent per click (must match Python)
// -------------------------------------------------------------------------

// ---- Display driver ------------------------------------------------------
// Two speed-ups over the library:
// 1. The "set window" command goes out as 5 SPI transfers instead of 11.
// 2. Pixels go out as 16-bit SPI words (SPI16). The Zephyr SPI driver spends
//    a fixed time per word, not per bit, so 16-bit words should move pixels
//    about twice as fast as the library's 8-bit ones. Set SPI16 = false to
//    compare, or if the screen shows garbage.
#if defined(ARDUINO_ARCH_ZEPHYR) && !defined(UI_SIM)
#define HAVE_SPI16 1
// Reads the Zephyr device out of the Arduino SPI object (protected member).
struct SpiPeek : public arduino::ZephyrSPI {
  static const struct device *dev(arduino::ZephyrSPI &s) { return static_cast<SpiPeek &>(s).spi_dev; }
};
#endif

class FastTFT : public DIYables_ST7789_SPI {
public:
  using DIYables_ST7789_SPI::DIYables_ST7789_SPI;
  bool spi16 = false;

  void begin16(bool enable, uint32_t freq) {
#ifdef HAVE_SPI16
    dev = SpiPeek::dev(SPI);
    memset(&cfg16, 0, sizeof(cfg16));
    cfg16.frequency = freq;
    cfg16.operation = SPI_OP_MODE_MASTER | SPI_TRANSFER_MSB | SPI_WORD_SET(16) | SPI_HOLD_ON_CS;
    spi16 = enable && dev != nullptr;
#endif
  }

  void window(int16_t x0, int16_t y0, int16_t x1, int16_t y1) {
    uint8_t b[4];
    writeCommand(0x2A);
    b[0] = x0 >> 8; b[1] = x0; b[2] = x1 >> 8; b[3] = x1;
    spiWriteBlock(b, 4);
    writeCommand(0x2B);
    b[0] = y0 >> 8; b[1] = y0; b[2] = y1 >> 8; b[3] = y1;
    spiWriteBlock(b, 4);
    writeCommand(0x2C);
  }

  // Pixels in native RGB565 order. The buffer may be modified.
  void sendPixels(uint16_t *px, size_t n) {
#ifdef HAVE_SPI16
    if (spi16) {
      struct spi_buf b = {.buf = px, .len = n * 2};
      struct spi_buf_set set = {.buffers = &b, .count = 1};
      spi_write(dev, &cfg16, &set);
      return;
    }
#endif
    uint8_t *p = (uint8_t *)px;                  // 8-bit: big-endian bytes
    for (size_t i = 0; i < n; i++) { uint16_t c = px[i]; p[2 * i] = c >> 8; p[2 * i + 1] = c & 0xFF; }
    while (n) {
      size_t k = n > 2048 ? 2048 : n;
      spiWriteBlock(p, k * 2);
      p += k * 2;
      n -= k;
    }
  }

  void writePixel(int16_t x, int16_t y, uint16_t c) override {
    if (x < 0 || y < 0 || x >= width() || y >= height()) return;
    window(x, y, x, y);
    sendPixels(&c, 1);
  }
  void drawPixel(int16_t x, int16_t y, uint16_t c) override {
    startWrite(); writePixel(x, y, c); endWrite();
  }
  void writeFillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) override {
    if (x < 0) { w += x; x = 0; }
    if (y < 0) { h += y; y = 0; }
    if (x + w > width()) w = width() - x;
    if (y + h > height()) h = height() - y;
    if (w <= 0 || h <= 0) return;
    window(x, y, x + w - 1, y + h - 1);
    fillColor(c, (uint32_t)w * h);
  }
  void fillRect(int16_t x, int16_t y, int16_t w, int16_t h, uint16_t c) override {
    startWrite(); writeFillRect(x, y, w, h, c); endWrite();
  }

protected:
  void fillColor(uint16_t c, uint32_t count) override {
    static uint16_t buf[1024];
    while (count) {
      uint32_t n = count > 1024 ? 1024 : count;
      for (uint32_t i = 0; i < n; i++) buf[i] = c;   // refilled: the 8-bit path swaps it
      sendPixels(buf, n);
      count -= n;
    }
  }

private:
#ifdef HAVE_SPI16
  const struct device *dev = nullptr;
  struct spi_config cfg16;
#endif
};

// ---- Types (declared before any function, see the Arduino builder) ------
struct Track {
  const char *artist, *title, *album;
  int16_t year, dur;
  uint8_t no, of;
};
struct Station { const char *name, *sub, *fmt; };
struct Album { const char *artist, *name; int16_t year; };
struct Level { uint8_t id; int ctx; int cursor; int total; char title[40]; };   // total -1 = not known yet
struct InItem { int8_t kind; int list, ctx, index, total; char text[68]; };     // kind 0 page, 1 item
struct RainCol { int16_t head; uint8_t len, period, phase; };
struct Marquee { bool on; int x, y, w, h, padY; uint8_t size; int maxOff, off; uint8_t phase; unsigned long at; };
struct NowInfo {
  char artist[48], title[72], album[56], fmt[16], src[8];
  int year, no, of, dur;
  bool radio;
};

enum { L_ROOT, L_QUEUE, L_ARTISTS, L_ARTIST_ALBUMS, L_ALBUMS, L_RADIO, L_CD, L_USB };
enum { V_NOW, V_SPECTRUM, V_VU, V_EYE };

// ---- Menu ----------------------------------------------------------------
// List ids, shared with Python: 1 queue, 2 artists, 3 an artist's albums,
// 4 albums, 5 radio, 6 CD. The top level (0) is drawn by the sketch itself.
const char *const ROOT_ITEMS[] = {"Queue", "Artists", "Albums", "Radio", "CD", "USB"};
const uint8_t ROOT_LISTS[] = {L_QUEUE, L_ARTISTS, L_ALBUMS, L_RADIO, L_CD, L_USB};
const int ROOT_N = sizeof(ROOT_LISTS) / sizeof(ROOT_LISTS[0]);
// Devices Python reports with dev(mask); a root item whose device is missing
// is drawn dim and only shows a hint.
const int DEV_CD = 1, DEV_USB = 2;

// ---- Display, colours ----------------------------------------------------
FastTFT tft(240, 320, TFT_CS, TFT_DC, TFT_RST);

#define RGB(r, g, b) DIYables_TFT_SPI::colorRGB(r, g, b)
const uint16_t BLACK = 0;
const uint16_t VFD   = RGB(0, 255, 120);
const uint16_t MUTED = RGB(0, 153, 72);
const uint16_t DIM   = RGB(0, 46, 22);
const uint16_t LINE  = RGB(0, 64, 30);
const uint16_t AMB   = RGB(255, 170, 40);
const uint16_t RED   = RGB(250, 60, 40);
const uint16_t GRN   = RGB(60, 230, 40);
const uint16_t YEL   = RGB(250, 220, 40);
const uint16_t DGRN  = RGB(10, 45, 8);
const uint16_t DYEL  = RGB(50, 44, 8);
const uint16_t DRED  = RGB(55, 12, 8);
const uint16_t PANEL = RGB(24, 17, 6);
const uint16_t NEEDLE = RGB(240, 225, 200);
const uint16_t PIVOT = RGB(80, 62, 34);
const uint16_t EYE_G = RGB(150, 255, 30);
const uint16_t EYE_D = RGB(22, 34, 6);
const uint16_t EYE_RIM = RGB(30, 44, 10);
const uint16_t EYE_HUB = RGB(10, 14, 4);
const uint16_t MX_HEAD = RGB(190, 255, 210);
const uint16_t MX_MID = RGB(0, 150, 70);
const uint16_t MX_DARK = RGB(0, 70, 32);
const uint16_t MX_CLOCK = RGB(150, 255, 190);
const uint16_t EYE_DOT = RGB(90, 140, 20);

// ---- Layout --------------------------------------------------------------
const int HEAD_H = 21;           // header 0..20, line at 21
const int CONTENT_Y = 22;        // content 22..195
const int CONTENT_H = 174;
const int FOOT_LINE = 196;       // footer below
const int VOL_SEGS = 25, VOL_X = 48, VOL_Y = 212, VOL_SW = 9;
const int ROWS = 8, ROW_Y0 = 24, ROW_H = 21, ROW_W = 306;

// ---- State ---------------------------------------------------------------
NowInfo now;
int curSong = -1;                // queue position of the current song, from MPD
bool playing = false;
int mpdState = 0;                // 0 stop, 1 play, 2 pause
bool rptOn = false, rndOn = false;
int elapsed = 0;
unsigned long tickAt = 0;
int volume = 0;
bool volKnown = false;           // false: MPD has no mixer (sound card unplugged) -> "VOL --"
unsigned long volLocalAt = 0;    // last volume change made with the knob
int clockBase = -1;              // minutes since midnight from Python, -1 = unknown
unsigned long clockAt = 0;
unsigned long levelsAt = 0;

// Inbox: written by the Bridge thread, read by loop()
struct InTrack { char src[8], fmt[16], artist[48], title[72], album[56]; int year, no, of, dur, radio; };
InTrack inTrack;
volatile bool inTrackNew = false;
int inSt[6];
volatile bool inStNew = false;
uint8_t inLv[18];
volatile bool inLvNew = false;
volatile int inClk = -1;
volatile int inDev = -1;
int devMask = DEV_CD | DEV_USB;            // until Python says otherwise
InItem inQ[16];                  // menu page/item messages, a small ring buffer
volatile uint8_t inQHead = 0, inQTail = 0;
char pageText[8][68];            // the 8 rows on screen
bool pageHave[8];
Arduino_LED_Matrix matrix;
int view = V_NOW;
bool inMenu = false;
Level st[4];
int depth = 0;
int rowW[ROWS];
unsigned long toastUntil = 0;
int volSegsShown = -1;
int progPx = -1;
int clockShown = -1;
unsigned long lastFrame = 0;
bool onBlack = false;            // drawing on a cleared area: text boxes only as wide as the text

// Animation state
float lv[16], lvL, lvR;
float barH[16], pkH[16];
uint8_t pkAge[16];
uint32_t segMask[16];
float vuV[2], vuShown[2];
float eyeV;
int shK;
uint32_t rng = 0x12345678;

// Idle screen (digital rain)
const int MX_COLS = 26, MX_ROWS = 15, MX_CW = 12, MX_CH = 16, MX_X0 = 4;
const int CLOCK_EVERY_MS = 20000;
bool idle = false;
bool swallowRelease = false;
unsigned long pausedSince = 0, lastInputAt = 0, rainFrameAt = 0, clockPhaseAt = 0;
uint8_t clockPhase = 0;          // 0 rain only, 1 time appears, 2 time shown, 3 time dissolves
RainCol rain[MX_COLS];
uint8_t mxGlyph[MX_ROWS][MX_COLS];
uint8_t mxLevel[MX_ROWS][MX_COLS];   // 0 empty, 1 dark, 2 mid, 3 bright, 4 head
uint8_t mxLock[MX_ROWS][MX_COLS];    // clock cells: 1 waiting for the rain, 2 lit

// Text renderer buffer: one strip of pixels
uint16_t pbuf[8192];

// ---- Input (interrupt driven) --------------------------------------------
const int8_t QUAD[16] = {0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0};
volatile uint8_t encState = 0;
uint8_t encRest = 3;                       // A/B levels when the knob sits in a detent
volatile int8_t qN = 0, qP = 0;            // edges since the last detent: plain / pressed
volatile int16_t detN = 0, detP = 0;       // whole clicks not yet handled
volatile bool pushDown = false, k0Down = false, turnedP = false;
volatile uint32_t pushEdgeAt = 0, k0EdgeAt = 0, pushPressAt = 0, pushReleaseAt = 0;
volatile uint16_t pushPresses = 0, pushReleases = 0, k0Presses = 0;

// Edges -> clicks, rounded to the nearest whole click.
int8_t clicksOf(int8_t q) {
  const int8_t h = STEPS_PER_DETENT / 2;
  return q >= 0 ? (q + h) / STEPS_PER_DETENT : -((-q + h) / STEPS_PER_DETENT);
}

// Edges are counted between detents, and the count is settled each time the
// knob comes to rest in a detent. A missed edge (contact bounce) then can't
// pile up: the next detent re-aligns the count.
void encoderISR() {
  encState = ((encState << 2) | (digitalRead(PIN_A) << 1) | digitalRead(PIN_B)) & 0x0F;
  int8_t d = QUAD[encState];
  if (REVERSE) d = -d;
  bool rest = (encState & 3) == encRest;
  if (pushDown) {                  // turning with the button held
    qP += d;
    if (rest) { int8_t c = clicksOf(qP); if (c) { detP += c; turnedP = true; } qP = 0; }
  } else {
    qN += d;
    if (rest) { detN += clicksOf(qN); qN = 0; }
  }
}

// Also called from loop(): catches anything an interrupt missed.
void pushISR() {
  uint32_t t = millis();
  bool down = !digitalRead(PIN_PUSH);
  if (down == pushDown || t - pushEdgeAt < DEBOUNCE_MS) return;
  pushEdgeAt = t;
  pushDown = down;
  if (down) { pushPressAt = t; turnedP = false; qP = 0; pushPresses++; }
  else      { pushReleaseAt = t; pushReleases++; }
}

void k0ISR() {
  uint32_t t = millis();
  bool down = !digitalRead(PIN_K0);
  if (down == k0Down || t - k0EdgeAt < DEBOUNCE_MS) return;
  k0EdgeAt = t;
  k0Down = down;
  if (down) k0Presses++;
}

// ---- Small helpers -------------------------------------------------------
int imin(int a, int b) { return a < b ? a : b; }
int imax(int a, int b) { return a > b ? a : b; }
float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
float frand() { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return (rng & 0xFFFF) / 65535.0f; }

// ---- UTF-8 text (Latin + Cyrillic) ----
// Characters in a UTF-8 string (continuation bytes are not counted).
int u8len(const char *s) {
  int n = 0;
  for (; *s; s++) if (((uint8_t)*s & 0xC0) != 0x80) n++;
  return n;
}

// Decodes UTF-8 into glyph pointers; returns the number of characters.
// ASCII and Cyrillic (А..я, Ё, ё) have glyphs, anything else shows as '?'.
int decodeGlyphs(const char *s, const uint8_t **out, int max) {
  const uint8_t *p = (const uint8_t *)s;
  int n = 0;
  while (*p && n < max) {
    uint32_t c = *p++;
    if (c >= 0xC0 && c < 0xE0 && (*p & 0xC0) == 0x80) c = ((c & 0x1F) << 6) | (*p++ & 0x3F);
    else if (c >= 0x80) { while ((*p & 0xC0) == 0x80) p++; c = '?'; }
    if (c < 0x80) out[n++] = &FONT5X7[c * 5];
    else if (c >= 0x410 && c < 0x450) out[n++] = &FONT5X7_CYR[(c - 0x410) * 5];
    else if (c == 0x401) out[n++] = &FONT5X7_CYR[64 * 5];
    else if (c == 0x451) out[n++] = &FONT5X7_CYR[65 * 5];
    else out[n++] = &FONT5X7['?' * 5];
  }
  return n;
}

// Upper case for Latin and Cyrillic, UTF-8 in and out.
void upper(const char *s, char *out, size_t n) {
  const uint8_t *p = (const uint8_t *)s;
  size_t i = 0;
  while (*p && i + 2 < n) {
    uint8_t a = *p++;
    if (a == 0xD0 && *p >= 0xB0 && *p <= 0xBF) { out[i++] = 0xD0; out[i++] = *p++ - 0x20; }      // а..п
    else if (a == 0xD1 && *p >= 0x80 && *p <= 0x8F) { out[i++] = 0xD0; out[i++] = *p++ + 0x20; } // р..я
    else if (a == 0xD1 && *p == 0x91) { out[i++] = 0xD0; out[i++] = 0x81; p++; }                  // ё
    else out[i++] = a < 0x80 ? toupper(a) : a;
  }
  out[i] = 0;
}

void fmtTime(int sec, char *out, size_t n) {
  snprintf(out, n, "%d:%02d", sec / 60, sec % 60);
}

// Split s into two lines of at most n characters, breaking at a space.
void wrap2(const char *s, int n, char *l1, char *l2, size_t cap) {
  if (u8len(s) <= n) { snprintf(l1, cap, "%s", s); l2[0] = 0; return; }
  int chars = 0, cut = 0, atN = 0;           // byte offsets
  for (int i = 0; s[i]; i++) {
    if (((uint8_t)s[i] & 0xC0) == 0x80) continue;
    if (chars == n) { atN = i; if (s[i] == ' ') cut = i; break; }
    if (s[i] == ' ') cut = i;
    chars++;
  }
  if (cut == 0) cut = atN;
  snprintf(l1, cap, "%.*s", cut, s);
  const char *rest = s + cut;
  while (*rest == ' ') rest++;
  snprintf(l2, cap, "%s", rest);
}

// ---- Text ----------------------------------------------------------------
// Draws a w x h box filled with bg, with the text at (padX, padY) inside it.
// The text is clipped to whole characters that fit. One SPI burst per strip.
// With `scroll` > 0 the text is shifted left by that many pixels and cut
// at the box edges mid-character (the marquee); otherwise only whole
// characters that fit are drawn.
void textBoxAt(int x, int y, int w, int h, int padX, int padY,
               const char *s, uint8_t size, uint16_t fg, uint16_t bg, int scroll) {
  if (w <= 0 || h <= 0) return;
  if (x + w > 320) w = 320 - x;
  if (y + h > 240) h = 240 - y;
  int cw = 6 * size;
  const uint8_t *gl[72];
  int len = decodeGlyphs(s, gl, 72);
  int fit = padX < w ? (w - padX) / cw : 0;
  if (scroll <= 0 && len > fit) len = fit;
  int stripRows = (int)(sizeof(pbuf) / sizeof(pbuf[0])) / w;

  tft.startWrite();
  tft.window(x, y, x + w - 1, y + h - 1);
  for (int row = 0; row < h; row += stripRows) {
    int n = imin(stripRows, h - row);
    uint16_t *p = pbuf;
    for (int r = row; r < row + n; r++) {
      int gy = r - padY;
      bool inY = gy >= 0 && gy < 8 * size;
      gy = inY ? gy / size : 0;
      for (int c = 0; c < w; c++) {
        bool on = false;
        if (inY) {
          int gx = c - padX + scroll;
          if (gx >= 0) {
            gx /= size;
            int ch = gx / 6, col = gx % 6;
            if (ch < len && col < 5)
              on = (gl[ch][col] >> gy) & 1;
          }
        }
        *p++ = on ? fg : bg;
      }
    }
    tft.sendPixels(pbuf, (size_t)n * w);
  }
  tft.endWrite();
}

void textBox(int x, int y, int w, int h, int padX, int padY,
             const char *s, uint8_t size, uint16_t fg, uint16_t bg) {
  textBoxAt(x, y, w, h, padX, padY, s, size, fg, bg, 0);
}

// Text exactly as wide as the box, starting at x.
void text(int x, int y, int w, const char *s, uint8_t size, uint16_t fg, uint16_t bg) {
  textBox(x, y, w, 8 * size, 0, 0, s, size, fg, bg);
}

// Right-aligned: text ends `margin` px before the box's right edge.
void textR(int x, int y, int w, int h, int padY, int margin,
           const char *s, uint8_t size, uint16_t fg, uint16_t bg) {
  int pad = imax(0, w - margin - u8len(s) * 6 * size);
  textBox(x, y, w, h, pad, padY, s, size, fg, bg);
}

// Box width for a text line: the full width when replacing older text,
// just the text itself when the area is known to be black already.
int fitW(int full, int padX, const char *s, uint8_t size) {
  return onBlack ? imin(full, padX + u8len(s) * 6 * size) : full;
}

void eraseText(int x, int y, int h, int padX, const char *s, uint8_t size, int maxW) {
  int w = imin(maxW, padX + u8len(s) * 6 * size);
  if (w > 0) tft.fillRect(x, y, w, h, BLACK);
}

void showMs(unsigned long ms) {
  if (!SHOW_MS) return;
  char b[10];
  snprintf(b, sizeof(b), "%lums", ms);
  textR(276, 230, 44, 8, 0, 2, b, 1, MUTED, BLACK);
}

// ---- Header and footer (views) -------------------------------------------
void headerLeft() {
  char b[16];
  snprintf(b, sizeof(b), "%s %s", now.src, mpdState == 1 ? "\x10" : mpdState == 2 ? "||" : "--");
  textBox(0, 0, 106, HEAD_H, 6, 3, b, 2, VFD, BLACK);
}

// Minutes since midnight, counted on from the last time Python sent it.
int clockNow() {
  if (clockBase < 0) return -1;
  return (clockBase + (millis() - clockAt) / 60000) % (24 * 60);
}

void headerClock() {
  int m = clockNow();
  clockShown = m;
  char b[8];
  if (m < 0) snprintf(b, sizeof(b), "--:--");
  else snprintf(b, sizeof(b), "%02d:%02d", m / 60, m % 60);
  textBox(106, 0, 74, HEAD_H, 7, 3, b, 2, MUTED, BLACK);
}

void headerRight() {
  textR(180, 0, 140, HEAD_H, 3, 4, now.fmt, 2, VFD, BLACK);
}

void drawHeader() {
  headerLeft();
  headerClock();
  headerRight();
  tft.fillRect(0, HEAD_H, 320, 1, LINE);
}

void drawVolume() {
  int lit = volKnown ? (volume * VOL_SEGS + 50) / 100 : 0;
  int from = 0, to = VOL_SEGS;
  if (volSegsShown >= 0) { from = imin(lit, volSegsShown); to = imax(lit, volSegsShown); }
  for (int i = from; i < to; i++)
    tft.fillRect(VOL_X + i * VOL_SW, VOL_Y, VOL_SW - 3, 12, i < lit ? VFD : DIM);
  volSegsShown = lit;
  char b[8];
  if (volKnown) snprintf(b, sizeof(b), "%3d", volume);
  else snprintf(b, sizeof(b), " --");
  textBox(278, 210, 42, 16, 2, 0, b, 2, volKnown ? VFD : MUTED, BLACK);
}

void drawFooter() {
  tft.fillRect(0, FOOT_LINE, 320, 1, LINE);
  textBox(0, 210, 44, 16, 4, 0, "VOL", 2, MUTED, BLACK);
  volSegsShown = -1;
  drawVolume();
}

void clearContent() {
  tft.fillRect(0, CONTENT_Y, 320, CONTENT_H, BLACK);
}

// Progress bar, only the changed part unless `full`.
void drawProgress(int x, int y, int w, bool full) {
  int px = now.dur > 0 ? (long)imin(elapsed, now.dur) * w / now.dur : 0;
  if (full || progPx < 0) {
    if (px > 0) tft.fillRect(x, y, px, 4, VFD);
    tft.fillRect(x + px, y, w - px, 4, DIM);
  } else if (px > progPx) {
    tft.fillRect(x + progPx, y, px - progPx, 4, VFD);
  } else if (px < progPx) {
    tft.fillRect(x + px, y, progPx - px, 4, DIM);
  }
  progPx = px;
}

// ---- View 1: Now playing -------------------------------------------------
void nowState() {
  textBox(0, 122, 200, 20, 8, 2,
          mpdState == 1 ? "\x10 PLAYING" : mpdState == 2 ? "|| PAUSED" : "-- STOPPED", 2, VFD, BLACK);
  char b[24];
  if (now.radio) snprintf(b, sizeof(b), "LIVE");
  else snprintf(b, sizeof(b), "RPT %s  RND %s", rptOn ? "on " : "off", rndOn ? "on " : "off");
  textR(200, 122, 120, 20, 7, 8, b, 1, MUTED, BLACK);
}

void nowTime(bool full) {
  char a[12], b[12];
  fmtTime(elapsed, a, sizeof(a));
  textBox(0, 162, 160, 16, 8, 0, a, 2, MUTED, BLACK);
  if (full) {
    if (now.dur > 0) fmtTime(now.dur, b, sizeof(b)); else snprintf(b, sizeof(b), "LIVE");
    textR(160, 162, 160, 16, 0, 8, b, 2, MUTED, BLACK);
  }
  drawProgress(8, 152, 304, full);
}

void nowMeta(char *b, size_t n) {
  char y[8] = "", t[16] = "";
  if (now.radio) { snprintf(b, n, "internet radio"); return; }
  if (now.year > 0) snprintf(y, sizeof(y), "%d   ", now.year);
  if (now.no > 0 && now.of > 0) snprintf(t, sizeof(t), "track %d/%d", now.no, now.of);
  else if (now.no > 0) snprintf(t, sizeof(t), "track %d", now.no);
  snprintf(b, n, "%s%s", y, t);
}

void nowStatic() {
  char b[64];
  upper(now.artist, b, sizeof(b));
  textBox(0, 26, fitW(320, 8, b, 2), 18, 8, 1, b, 2, MUTED, BLACK);
  textBox(0, 46, fitW(320, 8, now.title, 3), 28, 8, 2, now.title, 3, VFD, BLACK);
  textBox(0, 76, fitW(320, 8, now.album, 2), 18, 8, 1, now.album, 2, VFD, BLACK);
  nowMeta(b, sizeof(b));
  textBox(0, 96, fitW(320, 8, b, 2), 18, 8, 1, b, 2, MUTED, BLACK);
  nowState();
  progPx = -1;
  nowTime(true);
}

// ---- View 2: Spectrum ----------------------------------------------------
const int SP_BARS = 16, SP_SEGS = 16, SP_X0 = 8, SP_BW = 19, SP_TOP = 28, SP_SH = 9;

void specSeg(int b, int s, bool lit) {
  uint16_t c;
  if (s < 11)      c = lit ? GRN : DGRN;
  else if (s < 14) c = lit ? YEL : DYEL;
  else             c = lit ? RED : DRED;
  tft.fillRect(SP_X0 + b * SP_BW, SP_TOP + (SP_SEGS - 1 - s) * SP_SH, SP_BW - 3, SP_SH - 2, c);
}

void specStatic() {
  for (int b = 0; b < SP_BARS; b++) {
    for (int s = 0; s < SP_SEGS; s++) specSeg(b, s, false);
    barH[b] = pkH[b] = 0;
    pkAge[b] = 0;
    segMask[b] = 0;
  }
  struct { int bar; const char *t; } labels[] = {
    {0, "31"}, {2, "70"}, {4, "160"}, {6, "380"}, {8, "870"},
    {10, "2k"}, {12, "4.5k"}, {15, "16k"},
  };
  for (auto &l : labels) {
    int w = strlen(l.t) * 6;
    int cx = SP_X0 + l.bar * SP_BW + (SP_BW - 3) / 2;
    text(cx - w / 2, SP_TOP + SP_SEGS * SP_SH + 4, w, l.t, 1, MUTED, BLACK);
  }
}

void specFrame() {
  for (int b = 0; b < SP_BARS; b++) {
    float target = lv[b] * SP_SEGS;
    barH[b] = fmaxf(target, barH[b] - 0.7f);
    if (barH[b] >= pkH[b]) { pkH[b] = barH[b]; pkAge[b] = 0; }
    else if (++pkAge[b] > 18) pkH[b] = fmaxf(pkH[b] - 0.3f, 0);
    int h = (int)lroundf(barH[b]), p = (int)lroundf(pkH[b]);
    uint32_t m = h > 0 ? (1UL << h) - 1 : 0;
    if (p > h && p > 0) m |= 1UL << (p - 1);
    uint32_t diff = m ^ segMask[b];
    for (int s = 0; s < SP_SEGS; s++)
      if (diff & (1UL << s)) specSeg(b, s, m & (1UL << s));
    segMask[b] = m;
  }
}

// ---- View 3: VU meters ---------------------------------------------------
const int VU_W = 148, VU_H = 118, VU_Y0 = 26, VU_R = 78;
int vuX0(int i) { return 8 + i * 156; }
int vuCx(int i) { return vuX0(i) + VU_W / 2; }
const int VU_CY = VU_Y0 + VU_H - 12;
const float VU_MIN = -45, VU_MAX = 45;

void vuLine(int i, float deg, int r0, int r1, uint16_t c) {
  float a = deg * (float)M_PI / 180;
  int cx = vuCx(i);
  int x0 = lroundf(cx + r0 * sinf(a)), y0 = lroundf(VU_CY - r0 * cosf(a));
  int x1 = lroundf(cx + r1 * sinf(a)), y1 = lroundf(VU_CY - r1 * cosf(a));
  tft.drawLine(x0, y0, x1, y1, c);
  tft.drawLine(x0 + 1, y0, x1 + 1, y1, c);
}

const char *vuLines(char *b, size_t n) {
  snprintf(b, n, "%s", now.artist[0] ? now.artist : now.album);
  return now.title;
}

void vuText() {
  char b[64];
  const char *a = vuLines(b, sizeof(b));
  textBox(0, 152, fitW(320, 8, a, 2), 18, 8, 1, a, 2, VFD, BLACK);
  textBox(0, 172, fitW(320, 8, b, 2), 18, 8, 1, b, 2, MUTED, BLACK);
}

void vuStatic() {
  struct { int k; const char *t; } marks[] = {{0, "-20"}, {4, "-10"}, {8, "-5"}, {13, "0"}, {20, "+3"}};
  for (int i = 0; i < 2; i++) {
    int x0 = vuX0(i), cx = vuCx(i);
    tft.fillRoundRect(x0, VU_Y0, VU_W, VU_H, 6, PANEL);
    for (int k = 0; k <= 20; k++)
      vuLine(i, VU_MIN + k * 4.5f, VU_R - (k % 2 ? 5 : 9), VU_R, k > 13 ? RED : AMB);
    for (auto &m : marks) {
      float a = (VU_MIN + m.k * 4.5f) * (float)M_PI / 180;
      int w = strlen(m.t) * 6;
      int x = lroundf(cx + (VU_R + 10) * sinf(a)) - w / 2;
      int y = lroundf(VU_CY - (VU_R + 10) * cosf(a)) - 4;
      text(x, y, w, m.t, 1, m.k > 13 ? RED : AMB, PANEL);
    }
    text(x0 + 8, VU_Y0 + VU_H - 22, 12, i ? "R" : "L", 2, AMB, PANEL);
    tft.fillCircle(cx, VU_CY, 5, PIVOT);
    vuV[i] = 0;
    vuShown[i] = VU_MIN;
    vuLine(i, VU_MIN, 8, VU_R - 12, NEEDLE);
  }
  vuText();
}

void vuFrame() {
  float target[2] = {lvL, lvR};
  for (int i = 0; i < 2; i++) {
    float t = clampf(target[i], 0, 1);
    vuV[i] += (t - vuV[i]) * (t > vuV[i] ? 0.30f : 0.10f);
    float a = VU_MIN + vuV[i] * (VU_MAX - VU_MIN);
    if (fabsf(a - vuShown[i]) < 0.8f) continue;
    vuLine(i, vuShown[i], 8, VU_R - 12, PANEL);
    vuLine(i, a, 8, VU_R - 12, NEEDLE);
    vuShown[i] = a;
  }
}

// ---- View 4: Magic eye ---------------------------------------------------
const int EYE_CX = 82, EYE_CY = 110, EYE_R = 68, EYE_HUB_R = 20;
const int EYE_TX = 166, EYE_TW = 154;

// Paints the shadow (dark) or the glow (green) between steps k0..k1 on both
// sides of the top centre line; one step = 2 degrees. Only the ring between
// the hub and the rim is painted, so the hub and its dot are never touched.
// Each piece is at most 10 degrees (the chord stays within 0.3 px of the
// circle) and overlaps 1 degree into the area that already has its colour.
// A piece is drawn as 4 triangles (both diagonals), because Adafruit's
// triangles can leave hairline gaps along a shared edge.
void eyePoint(float deg, int r, int &x, int &y) {
  float a = deg * (float)M_PI / 180;
  x = lroundf(EYE_CX + r * sinf(a));
  y = lroundf(EYE_CY - r * cosf(a));
}

void eyeSector(int k0, int k1, uint16_t c) {
  bool dark = c == EYE_D;
  int ro = dark ? EYE_R + 1 : EYE_R, ri = EYE_HUB_R + 1;
  for (int k = k0; k < k1; k += 5) {
    int ke = imin(k + 5, k1);
    float d0 = k * 2 - (dark ? 1 : 0), d1 = ke * 2 + (dark ? 0 : 1);
    for (int side = -1; side <= 1; side += 2) {
      int ix0, iy0, ix1, iy1, ox0, oy0, ox1, oy1;
      eyePoint(side * d0, ri, ix0, iy0);
      eyePoint(side * d1, ri, ix1, iy1);
      eyePoint(side * d0, ro, ox0, oy0);
      eyePoint(side * d1, ro, ox1, oy1);
      tft.fillTriangle(ix0, iy0, ox0, oy0, ox1, oy1, c);
      tft.fillTriangle(ix0, iy0, ox1, oy1, ix1, iy1, c);
      tft.fillTriangle(ox0, oy0, ox1, oy1, ix1, iy1, c);
      tft.fillTriangle(ix0, iy0, ox0, oy0, ix1, iy1, c);
    }
  }
}

int eyeTargetK() {
  float half = 46 - clampf(eyeV, 0, 1) * 40;   // shadow half-angle, degrees
  return imax(3, imin(23, (int)lroundf(half / 2)));
}

// The title on the eye screen: one or two lines of 12; longer titles stay on
// one line and scroll (l1 = the whole title, l2 empty).
void eyeTitle(char *l1, char *l2, size_t cap) {
  wrap2(now.title, EYE_TW / 12, l1, l2, cap);
  if (u8len(l2) > EYE_TW / 12) { snprintf(l1, cap, "%s", now.title); l2[0] = 0; }
}

void eyeText() {
  char b[64], l1[80], l2[80];
  upper(now.artist, b, sizeof(b));
  textBox(EYE_TX, 32, fitW(EYE_TW, 0, b, 1), 10, 0, 1, b, 1, MUTED, BLACK);
  eyeTitle(l1, l2, sizeof(l1));
  textBox(EYE_TX, 46, fitW(EYE_TW, 0, l1, 2), 18, 0, 1, l1, 2, VFD, BLACK);
  textBox(EYE_TX, 64, fitW(EYE_TW, 0, l2, 2), 18, 0, 1, l2, 2, VFD, BLACK);
  wrap2(now.album, EYE_TW / 6, l1, l2, sizeof(l1));
  textBox(EYE_TX, 88, fitW(EYE_TW, 0, l1, 1), 10, 0, 1, l1, 1, VFD, BLACK);
  textBox(EYE_TX, 98, fitW(EYE_TW, 0, l2, 1), 10, 0, 1, l2, 1, VFD, BLACK);
}

void eyeTime(bool full) {
  char a[12], b[12];
  fmtTime(elapsed, a, sizeof(a));
  textBox(EYE_TX, 142, 72, 16, 0, 0, a, 2, MUTED, BLACK);
  if (full) {
    if (now.dur > 0) fmtTime(now.dur, b, sizeof(b)); else snprintf(b, sizeof(b), "LIVE");
    textR(EYE_TX + 72, 142, EYE_TW - 72, 16, 0, 8, b, 2, MUTED, BLACK);
  }
  drawProgress(EYE_TX, 132, EYE_TW - 8, full);
}

void eyeStatic() {
  tft.fillCircle(EYE_CX, EYE_CY, EYE_R + 4, EYE_RIM);
  tft.fillCircle(EYE_CX, EYE_CY, EYE_R, EYE_G);
  eyeV = 0;
  shK = eyeTargetK();
  eyeSector(0, shK, EYE_D);
  tft.fillCircle(EYE_CX, EYE_CY, EYE_HUB_R + 1, EYE_HUB);  // drawn once, never covered
  tft.fillCircle(EYE_CX, EYE_CY, 9, EYE_DOT);
  eyeText();
  progPx = -1;
  eyeTime(true);
}

void eyeFrame() {
  float t = clampf((lvL + lvR) / 2, 0, 1);
  eyeV += (t - eyeV) * (t > eyeV ? 0.35f : 0.12f);
  int k = eyeTargetK();
  if (k == shK) return;
  if (k < shK) eyeSector(k, shK, EYE_G);
  else         eyeSector(shK, k, EYE_D);
  shK = k;
}

// ---- Audio levels from Python ---------------------------------------------
// lv message: 16 spectrum bands + left + right, each 0..255. If nothing has
// arrived for a while (paused, stopped, Python busy), everything falls to 0.
void takeLevels() {
  if (millis() - levelsAt > 300) {
    for (int b = 0; b < 16; b++) lv[b] = 0;
    lvL = lvR = 0;
  }
}

// ---- Marquee: a long title swings left to its end and back ---------------
// Stands 2 s at each end, moves 3 px every 60 ms (~50 px/s). One step
// redraws just the glyph rows of the title.
//
// Drawn in vertical strips, left to right. The panel is a portrait one
// turned on its side, so it refreshes our picture column by column. A box
// sent row by row (top to bottom) would cross that refresh: the top of the
// letters already moved, the bottom not yet - a comb through the text.
// Strips sent along the refresh direction leave at most one vertical seam.
const unsigned long MQ_HOLD_MS = 2000, MQ_STEP_MS = 60;
const int MQ_PX = 3;
Marquee mq;

void marqueeSetup() {
  mq.on = false;
  if (inMenu || idle) return;
  int x, y, w, h, padY, size;
  if (view == V_NOW) { x = 8; y = 48; w = 304; h = 24; padY = 0; size = 3; }
  else if (view == V_EYE) {
    char l1[80], l2[80];
    eyeTitle(l1, l2, sizeof(l1));
    if (l2[0]) return;                         // fits on two lines
    x = EYE_TX; y = 47; w = EYE_TW; h = 16; padY = 0; size = 2;
  } else if (view == V_VU) { x = 8; y = 153; w = 312; h = 16; padY = 0; size = 2; }   // the static title runs to the screen edge
  else return;
  int textW = u8len(now.title) * 6 * size - size;   // the last glyph column is blank
  if (textW <= w) return;
  mq = {true, x, y, w, h, padY, (uint8_t)size, textW - w, 0, 0, millis()};
}

void marqueeDraw();

void marqueeStep() {
  if (!mq.on) return;
  unsigned long t = millis();
  if (mq.phase == 0 || mq.phase == 2) {        // standing at an end
    if (t - mq.at >= MQ_HOLD_MS) { mq.phase++; mq.at = t; }
    return;
  }
  if (t - mq.at < MQ_STEP_MS) return;
  mq.at = t;
  if (mq.phase == 1) { mq.off = imin(mq.maxOff, mq.off + MQ_PX); if (mq.off == mq.maxOff) mq.phase = 2; }
  else               { mq.off = imax(0, mq.off - MQ_PX);         if (mq.off == 0) mq.phase = 0; }
  marqueeDraw();
}

void marqueeDraw() {
  const uint8_t *gl[72];
  int len = decodeGlyphs(now.title, gl, 72);
  const int SW = 8;                            // strip width, px
  for (int sx = 0; sx < mq.w; sx += SW) {
    int sw = imin(SW, mq.w - sx);
    uint16_t *p = pbuf;
    for (int r = 0; r < mq.h; r++) {
      int gy = (r - mq.padY) / mq.size;
      for (int c = sx; c < sx + sw; c++) {
        int gx = (c + mq.off) / mq.size;
        int ch = gx / 6, col = gx % 6;
        bool on = r >= mq.padY && gy < 8 && ch < len && col < 5 && ((gl[ch][col] >> gy) & 1);
        *p++ = on ? VFD : BLACK;
      }
    }
    tft.startWrite();
    tft.window(mq.x + sx, mq.y, mq.x + sx + sw - 1, mq.y + mq.h - 1);
    tft.sendPixels(pbuf, (size_t)sw * mq.h);
    tft.endWrite();
  }
}

// ---- Erasing a view: only what it drew, not the whole area ----------------
void eraseView(int v) {
  char b[64], l1[80], l2[80];
  switch (v) {
    case V_NOW:
      upper(now.artist, b, sizeof(b));
      eraseText(0, 26, 18, 8, b, 2, 320);
      eraseText(0, 46, 28, 8, now.title, 3, 320);
      eraseText(0, 76, 18, 8, now.album, 2, 320);
      nowMeta(b, sizeof(b));
      eraseText(0, 96, 18, 8, b, 2, 320);
      tft.fillRect(0, 122, 8 + 10 * 12, 20, BLACK);
      tft.fillRect(200, 122, 120, 20, BLACK);
      tft.fillRect(8, 152, 304, 4, BLACK);
      tft.fillRect(0, 162, 320, 16, BLACK);
      break;
    case V_SPECTRUM:
      for (int i = 0; i < SP_BARS; i++)
        tft.fillRect(SP_X0 + i * SP_BW, SP_TOP, SP_BW - 3, SP_SEGS * SP_SH, BLACK);
      tft.fillRect(0, SP_TOP + SP_SEGS * SP_SH + 4, 320, 8, BLACK);
      break;
    case V_VU: {
      for (int i = 0; i < 2; i++) tft.fillRect(vuX0(i), VU_Y0, VU_W, VU_H, BLACK);
      const char *a = vuLines(b, sizeof(b));
      eraseText(0, 152, 18, 8, a, 2, 320);
      eraseText(0, 172, 18, 8, b, 2, 320);
      break;
    }
    case V_EYE:
      tft.fillCircle(EYE_CX, EYE_CY, EYE_R + 4, BLACK);
      upper(now.artist, b, sizeof(b));
      eraseText(EYE_TX, 32, 10, 0, b, 1, EYE_TW);
      eyeTitle(l1, l2, sizeof(l1));
      eraseText(EYE_TX, 46, 18, 0, l1, 2, EYE_TW);
      eraseText(EYE_TX, 64, 18, 0, l2, 2, EYE_TW);
      wrap2(now.album, EYE_TW / 6, l1, l2, sizeof(l1));
      eraseText(EYE_TX, 88, 10, 0, l1, 1, EYE_TW);
      eraseText(EYE_TX, 98, 10, 0, l2, 1, EYE_TW);
      tft.fillRect(EYE_TX, 132, EYE_TW, 26, BLACK);
      break;
  }
}

// ---- Idle screen: digital rain --------------------------------------------
// Columns of glyphs fall at different speeds: a pale head, a bright glyph
// behind it, then the trail fades through mid and dark green. Only the few
// cells that change are redrawn each frame. Every 20 s the time appears out
// of the rain: the cells that form the digits light up as the rain passes
// over them, stay for a few seconds, then dissolve.
int irand(int n) { rng ^= rng << 13; rng ^= rng >> 17; rng ^= rng << 5; return rng % n; }

const uint8_t *rainGlyph(uint8_t id) {
  return id < KANA_COUNT ? &FONT5X7_KANA[id * 5] : &FONT5X7[('0' + id - KANA_COUNT) * 5];
}

void mxDraw(int r, int c) {
  int x = MX_X0 + c * MX_CW, y = r * MX_CH;
  bool lit = mxLock[r][c] == 2;
  uint8_t lv = mxLevel[r][c];
  if (!lit && lv == 0) { tft.fillRect(x, y, MX_CW, MX_CH, BLACK); return; }
  if (clockPhase && !lit && lv > 2) lv = 2;      // while the time shows, the rain steps back
  uint16_t fg = lit ? MX_CLOCK : lv == 4 ? MX_HEAD : lv == 3 ? VFD : lv == 2 ? MX_MID : MX_DARK;
  const uint8_t *g = rainGlyph(mxGlyph[r][c]);
  uint16_t *p = pbuf;
  for (int py = 0; py < MX_CH; py++)
    for (int px = 0; px < MX_CW; px++) {
      int gx = px / 2, gy = py / 2;
      bool on = gx < 5 && ((g[4 - gx] >> gy) & 1);    // mirrored, as in the film
      *p++ = lit ? (on ? BLACK : MX_CLOCK) : (on ? fg : BLACK);   // clock cells: negative, so digits read as solid shapes
    }
  tft.startWrite();
  tft.window(x, y, x + MX_CW - 1, y + MX_CH - 1);
  tft.sendPixels(pbuf, MX_CW * MX_CH);
  tft.endWrite();
}

void mxSet(int r, int c, uint8_t lv) {
  if (r < 0 || r >= MX_ROWS) return;
  if (lv == 4) {
    mxGlyph[r][c] = irand(KANA_COUNT + 10);
    if (mxLock[r][c] == 1) mxLock[r][c] = 2;      // the rain "writes" the clock
  }
  if (mxLevel[r][c] == lv && lv != 4) return;
  mxLevel[r][c] = lv;
  mxDraw(r, c);
}

void rainReset(int c, bool first) {
  rain[c].head = first ? -irand(MX_ROWS * 2) : -irand(10);
  rain[c].len = 6 + irand(11);
  rain[c].period = 1 + irand(3);
  rain[c].phase = 0;
}

void rainStep(int c) {
  RainCol &k = rain[c];
  int h = ++k.head;
  mxSet(h, c, 4);
  mxSet(h - 1, c, 3);
  mxSet(h - 3, c, 2);
  if (k.len * 2 / 3 > 3) mxSet(h - k.len * 2 / 3, c, 1);
  mxSet(h - k.len, c, 0);
  if (h - k.len >= MX_ROWS) rainReset(c, false);
}

// Big digits, 3x5 cells each
const uint8_t BIG_DIGITS[10][5] = {
  {7, 5, 5, 5, 7}, {2, 6, 2, 2, 7}, {7, 1, 7, 4, 7}, {7, 1, 7, 1, 7}, {5, 5, 7, 1, 1},
  {7, 4, 7, 1, 7}, {7, 4, 7, 5, 7}, {7, 1, 1, 1, 1}, {7, 5, 7, 5, 7}, {7, 5, 7, 1, 7},
};

void clockTargets() {
  int m = clockNow();
  if (m < 0) return;
  int d[4] = {m / 600, m / 60 % 10, m % 60 / 10, m % 10};
  int x0[4] = {4, 8, 14, 18};
  const int top = 5;
  for (int i = 0; i < 4; i++)
    for (int r = 0; r < 5; r++)
      for (int b = 0; b < 3; b++)
        if (BIG_DIGITS[d[i]][r] & (4 >> b)) mxLock[top + r][x0[i] + b] = 1;
  mxLock[top + 1][12] = mxLock[top + 3][12] = 1;   // colon
}

void clockUpdate() {
  unsigned long t = millis() - clockPhaseAt;
  if (clockPhase == 0 && t >= CLOCK_EVERY_MS) {
    clockTargets();
    clockPhase = 1; clockPhaseAt = millis();
  } else if (clockPhase == 1 && t >= 1500) {         // light whatever the rain missed
    for (int r = 0; r < MX_ROWS; r++)
      for (int c = 0; c < MX_COLS; c++)
        if (mxLock[r][c] == 1) { mxLock[r][c] = 2; mxGlyph[r][c] = irand(KANA_COUNT + 10); mxDraw(r, c); }
    clockPhase = 2; clockPhaseAt = millis();
  } else if (clockPhase == 2 && t >= 3500) {
    clockPhase = 3; clockPhaseAt = millis();
  } else if (clockPhase == 3) {                       // dissolve over ~1 s
    int left = 0;
    for (int r = 0; r < MX_ROWS; r++)
      for (int c = 0; c < MX_COLS; c++)
        if (mxLock[r][c]) {
          if (irand(100) < 12) { mxLock[r][c] = 0; mxLevel[r][c] = 0; mxDraw(r, c); }
          else left++;
        }
    if (!left) { clockPhase = 0; clockPhaseAt = millis(); }
  }
}

void rainFrame() {
  for (int c = 0; c < MX_COLS; c++)
    if (++rain[c].phase >= rain[c].period) { rain[c].phase = 0; rainStep(c); }
  for (int i = 0; i < 3; i++) {                       // glyphs flicker in the trails
    int r = irand(MX_ROWS), c = irand(MX_COLS);
    if (!mxLock[r][c] && (mxLevel[r][c] == 2 || mxLevel[r][c] == 3)) {
      mxGlyph[r][c] = irand(KANA_COUNT + 10);
      mxDraw(r, c);
    }
  }
  clockUpdate();
}

void enterIdle() {
  idle = true;
  mq.on = false;
  inMenu = false;
  memset(mxLevel, 0, sizeof(mxLevel));
  memset(mxLock, 0, sizeof(mxLock));
  for (int c = 0; c < MX_COLS; c++) rainReset(c, true);
  clockPhase = 0;
  clockPhaseAt = millis() - (CLOCK_EVERY_MS - 8000);   // first time after ~8 s
  tft.fillScreen(BLACK);
}

// ---- Views ---------------------------------------------------------------
void drawViewContent() {
  switch (view) {
    case V_NOW: nowStatic(); break;
    case V_SPECTRUM: specStatic(); break;
    case V_VU: vuStatic(); break;
    case V_EYE: eyeStatic(); break;
  }
}

// Header, view and footer, drawn onto a content area that is already black.
void drawViewFull() {
  drawHeader();
  onBlack = true;
  drawViewContent();
  onBlack = false;
  drawFooter();
  marqueeSetup();
}

void switchView(int d) {
  unsigned long t0 = millis();
  eraseView(view);
  view = ((view + d) % 4 + 4) % 4;
  onBlack = true;
  drawViewContent();
  onBlack = false;
  marqueeSetup();
  showMs(millis() - t0);
}

// The visible view's track text, erased before a new track is drawn: with
// boxes only as wide as the text, a shorter title must not leave the end of
// the old one on screen.
void eraseTrackText() {
  if (view == V_NOW || view == V_EYE || view == V_VU) {
    // Same as erasing the view, but keep graphics (eye, meters) in place.
    char b[64], l1[80], l2[80];
    if (view == V_NOW) {
      upper(now.artist, b, sizeof(b));
      eraseText(0, 26, 18, 8, b, 2, 320);
      eraseText(0, 46, 28, 8, now.title, 3, 320);
      eraseText(0, 76, 18, 8, now.album, 2, 320);
      nowMeta(b, sizeof(b));
      eraseText(0, 96, 18, 8, b, 2, 320);
    } else if (view == V_EYE) {
      upper(now.artist, b, sizeof(b));
      eraseText(EYE_TX, 32, 10, 0, b, 1, EYE_TW);
      eyeTitle(l1, l2, sizeof(l1));
      eraseText(EYE_TX, 46, 18, 0, l1, 2, EYE_TW);
      eraseText(EYE_TX, 64, 18, 0, l2, 2, EYE_TW);
      wrap2(now.album, EYE_TW / 6, l1, l2, sizeof(l1));
      eraseText(EYE_TX, 88, 10, 0, l1, 1, EYE_TW);
      eraseText(EYE_TX, 98, 10, 0, l2, 1, EYE_TW);
    } else {
      const char *a = vuLines(b, sizeof(b));
      eraseText(0, 152, 18, 8, a, 2, 320);
      eraseText(0, 172, 18, 8, b, 2, 320);
    }
  }
  textR(180, 0, 140, HEAD_H, 3, 4, "", 2, VFD, BLACK);   // format in the header
}

void onTrackChanged() {
  if (inMenu || idle) return;
  headerLeft();
  headerRight();
  onBlack = true;                  // old text was just erased
  if (view == V_NOW) nowStatic();
  else if (view == V_EYE) { eyeText(); progPx = -1; eyeTime(true); }
  else if (view == V_VU) vuText();
  onBlack = false;
  marqueeSetup();
}

void onStateChanged() {
  if (inMenu || idle) return;
  headerLeft();
  if (view == V_NOW) nowState();
}

void onTimeTick() {
  if (inMenu || idle) return;
  if (view == V_NOW) nowTime(false);
  else if (view == V_EYE) eyeTime(false);
}

void exitIdle() {
  idle = false;
  tft.fillScreen(BLACK);
  drawViewFull();
}

// ---- Messages from Python (Bridge thread: copy only, never draw) ----------
void copyStr(char *dst, size_t n, const String &s) {
  snprintf(dst, n, "%s", s.c_str());
}

void onTrk(String src, String fmt, String artist, String title, String album,
           int year, int no, int of, int dur, int radio) {
  noInterrupts();
  copyStr(inTrack.src, sizeof(inTrack.src), src);
  copyStr(inTrack.fmt, sizeof(inTrack.fmt), fmt);
  copyStr(inTrack.artist, sizeof(inTrack.artist), artist);
  copyStr(inTrack.title, sizeof(inTrack.title), title);
  copyStr(inTrack.album, sizeof(inTrack.album), album);
  inTrack.year = year; inTrack.no = no; inTrack.of = of; inTrack.dur = dur; inTrack.radio = radio;
  inTrackNew = true;
  interrupts();
}

// state 0 stop / 1 play / 2 pause, elapsed seconds, volume 0..100 (-1 = none),
// repeat, random, queue position of the current song (-1 = none)
void onSt(int state, int el, int vol, int rpt, int rnd, int song) {
  noInterrupts();
  inSt[0] = state; inSt[1] = el; inSt[2] = vol; inSt[3] = rpt; inSt[4] = rnd; inSt[5] = song;
  inStNew = true;
  interrupts();
}

void onLv(std::vector<uint8_t> v) {
  if (v.size() != sizeof(inLv)) return;
  noInterrupts();
  memcpy(inLv, v.data(), sizeof(inLv));
  inLvNew = true;
  interrupts();
}

void onClk(int minutes) { inClk = minutes; }
void onDev(int mask) { inDev = mask; }

void pushItem(int8_t kind, int list, int ctx, int index, int total, const char *text) {
  noInterrupts();
  uint8_t next = (inQHead + 1) % 16;
  if (next != inQTail) {                       // full: drop (the page is re-requested on demand)
    InItem &it = inQ[inQHead];
    it.kind = kind; it.list = list; it.ctx = ctx; it.index = index; it.total = total;
    snprintf(it.text, sizeof(it.text), "%s", text);
    inQHead = next;
  }
  interrupts();
}

void onPage(int list, int ctx, int start, int total) { pushItem(0, list, ctx, start, total, ""); }
void onItem(int list, int ctx, int index, String text) { pushItem(1, list, ctx, index, 0, text.c_str()); }

// The LED matrix has its own driver, it can be drawn right here.
void draw(std::vector<uint8_t> frame) {
  if (frame.size() == 104) matrix.draw(frame.data());
}

// ---- Applying the inbox (loop) ---------------------------------------------
void applyTrack() {
  InTrack t;
  noInterrupts();
  t = inTrack;
  inTrackNew = false;
  interrupts();
  if (!inMenu && !idle) eraseTrackText();
  snprintf(now.src, sizeof(now.src), "%s", t.src);
  snprintf(now.fmt, sizeof(now.fmt), "%s", t.fmt);
  snprintf(now.artist, sizeof(now.artist), "%s", t.artist);
  snprintf(now.title, sizeof(now.title), "%s", t.title);
  snprintf(now.album, sizeof(now.album), "%s", t.album);
  now.year = t.year; now.no = t.no; now.of = t.of; now.dur = t.dur; now.radio = t.radio;
  onTrackChanged();
}

void applyStatus() {
  int s[6];
  noInterrupts();
  memcpy(s, inSt, sizeof(s));
  inStNew = false;
  interrupts();
  bool stateChanged = s[0] != mpdState || (s[3] != 0) != rptOn || (s[4] != 0) != rndOn;
  if (s[0] != 1 && mpdState == 1) pausedSince = millis();
  mpdState = s[0];
  playing = mpdState == 1;
  rptOn = s[3]; rndOn = s[4];
  curSong = s[5];
  elapsed = s[1];
  tickAt = millis();
  if (playing && idle) exitIdle();
  // The knob wins for a moment: replies to our own changes arrive late.
  if (s[2] < 0) {                              // no mixer
    if (volKnown) { volKnown = false; if (!inMenu && !idle) drawVolume(); }
  } else if (!volKnown || (s[2] != volume && millis() - volLocalAt > 800)) {
    volKnown = true;
    volume = s[2];
    if (!inMenu && !idle) drawVolume();
  }
  if (stateChanged) onStateChanged();
  onTimeTick();
}

void applyMenuItem(const InItem &it);
void menuDevicesChanged();

void applyInbox() {
  while (inQTail != inQHead) {
    InItem it;
    noInterrupts();
    it = inQ[inQTail];
    inQTail = (inQTail + 1) % 16;
    interrupts();
    applyMenuItem(it);
  }
  if (inTrackNew) applyTrack();
  if (inStNew) applyStatus();
  if (inLvNew) {
    noInterrupts();
    for (int b = 0; b < 16; b++) lv[b] = inLv[b] / 255.0f;
    lvL = inLv[16] / 255.0f;
    lvR = inLv[17] / 255.0f;
    inLvNew = false;
    interrupts();
    levelsAt = millis();
  }
  if (inClk >= 0) {
    clockBase = inClk; clockAt = millis(); inClk = -1;
    if (!inMenu && !idle) headerClock();
  }
  if (inDev >= 0) {
    int m = inDev; inDev = -1;
    if (m != devMask) { devMask = m; menuDevicesChanged(); }
  }
}

// ---- Commands to Python ------------------------------------------------------
const int CMD_TOGGLE = 1, CMD_NEXT = 2, CMD_PREV = 3;

void sendCmd(int code) { Bridge.notify("cmd", code); }

// ---- Menu data -----------------------------------------------------------
int listCount(const Level &l) {
  if (l.id == L_ROOT) return ROOT_N;
  return l.total;                              // -1 until Python answers
}

void listLabel(const Level &l, int i, char *out, size_t n) {
  int row = i - i / ROWS * ROWS;
  if (l.id == L_ROOT) snprintf(out, n, "%s", ROOT_ITEMS[i]);
  else if (l.total == 0) snprintf(out, n, l.id == L_CD ? "No disc" : "(empty)");
  else if (l.total < 0 && l.id == L_CD) snprintf(out, n, "Reading disc...");
  else snprintf(out, n, "%s", pageHave[row] ? pageText[row] : "...");
}

const char *listTitle(const Level &l) {
  switch (l.id) {
    case L_ROOT: return "MENU";
    case L_QUEUE: return "QUEUE";
    case L_ARTISTS: return "ARTISTS";
    case L_ARTIST_ALBUMS: return l.title;
    case L_ALBUMS: return "ALBUMS";
    case L_RADIO: return "RADIO";
    case L_USB: return "USB";
    default: return "CD";
  }
}

bool opensSubmenu(const Level &l) { return l.id == L_ROOT || l.id == L_ARTISTS; }
bool fromPython(const Level &l) { return l.id != L_ROOT; }

// Root items for devices that are not connected: drawn dim, no submenu.
bool rootAvailable(int i) {
  uint8_t id = ROOT_LISTS[i];
  if (id == L_CD) return devMask & DEV_CD;
  if (id == L_USB) return devMask & DEV_USB;
  return true;
}

// ---- Menu drawing --------------------------------------------------------
int pageTop(int c) { return c / ROWS * ROWS; }

void menuHeader() {
  Level &l = st[depth];
  char b[40];
  snprintf(b, sizeof(b), "< %s", listTitle(l));
  textBox(0, 0, 220, HEAD_H, 6, 3, b, 2, VFD, BLACK);
  int n = listCount(l);
  if (n > 0) snprintf(b, sizeof(b), "%d/%d", l.cursor + 1, n);
  else b[0] = 0;
  textR(220, 0, 100, HEAD_H, 3, 6, b, 2, MUTED, BLACK);
}

void menuRow(int i) {
  Level &l = st[depth];
  int idx = pageTop(l.cursor) + i;
  int y = ROW_Y0 + i * ROW_H;
  int n = listCount(l);
  if (n <= 0) n = 1;                           // loading ("...") or empty ("(empty)"): one row
  if (idx >= n) {
    if (rowW[i] > 0) tft.fillRect(0, y, rowW[i], ROW_H, BLACK);
    rowW[i] = 0;
    return;
  }
  char b[64];
  listLabel(l, idx, b, sizeof(b));
  bool off = l.id == L_ROOT && !rootAvailable(idx);
  uint16_t fg = off ? LINE : VFD, bar = off ? MUTED : VFD;
  if (idx == l.cursor) {
    tft.fillRect(0, y, 6, ROW_H, BLACK);
    textBox(6, y, ROW_W - 6, ROW_H - 1, 6, 2, b, 2, BLACK, bar);
    tft.fillRect(6, y + ROW_H - 1, ROW_W - 6, 1, BLACK);
    if (opensSubmenu(l) && !off) textBox(ROW_W - 18, y, 12, ROW_H - 1, 0, 2, ">", 2, BLACK, bar);
    rowW[i] = ROW_W;
  } else {
    int w = imin(ROW_W, 12 + u8len(b) * 12);
    textBox(0, y, imax(w, rowW[i]), ROW_H, 12, 2, b, 2, fg, BLACK);
    rowW[i] = w;
  }
}

void menuScrollbar() {
  Level &l = st[depth];
  int n = imax(0, listCount(l)), x = 310, y = ROW_Y0, h = ROWS * ROW_H - 1;
  tft.fillRect(ROW_W, y, 320 - ROW_W, h, BLACK);
  if (n <= ROWS) return;
  int pages = (n + ROWS - 1) / ROWS;
  int th = imax(8, h / pages);
  int ty = y + (long)(h - th) * (pageTop(l.cursor) / ROWS) / (pages - 1);
  tft.fillRect(x, y, 3, h, DIM);
  tft.fillRect(x, ty, 3, th, MUTED);
}

void menuRows();

// dev(mask) arrived: only the root page shows it.
void menuDevicesChanged() {
  if (inMenu && !idle && depth == 0) menuRows();
}

void menuHint() {
  textBox(0, 204, 276, 22, 8, 2, "press:open  K0:back", 2, MUTED, BLACK);
}

void toast(const char *msg) {
  textBox(0, 204, 276, 22, 8, 2, msg, 2, AMB, BLACK);
  toastUntil = millis() + 1500;
}

void menuRows() {
  for (int i = 0; i < ROWS; i++) menuRow(i);
  menuScrollbar();
}

// Ask Python for the page under the cursor; its rows show "..." until then.
void requestPage() {
  Level &l = st[depth];
  for (int i = 0; i < ROWS; i++) pageHave[i] = false;
  if (fromPython(l)) Bridge.notify("get", (int)l.id, l.ctx, pageTop(l.cursor));
}

void applyMenuItem(const InItem &it) {
  if (!inMenu || idle) return;
  Level &l = st[depth];
  if (it.list != l.id || it.ctx != l.ctx) return;           // an old answer
  if (it.kind == 0) {
    if (it.index != pageTop(l.cursor)) return;
    l.total = it.total;
    if (l.total > 0 && l.cursor >= l.total) {               // the list got shorter
      l.cursor = l.total - 1;
      requestPage();
    }
    menuHeader();
    menuRows();
  } else {
    int row = it.index - pageTop(l.cursor);
    if (row < 0 || row >= ROWS) return;
    snprintf(pageText[row], sizeof(pageText[row]), "%s", it.text);
    pageHave[row] = true;
    menuRow(row);
  }
}

void menuEnter() {
  unsigned long t0 = millis();
  inMenu = true;
  mq.on = false;
  depth = 0;
  st[0] = {L_ROOT, 0, 0, ROOT_N, ""};
  eraseView(view);
  for (int i = 0; i < ROWS; i++) rowW[i] = 0;       // rows area is black now
  tft.fillRect(276, 204, 44, 24, BLACK);           // volume number
  menuHeader();
  menuRows();
  menuHint();
  showMs(millis() - t0);
}

// A hint left over from the previous list would be misleading.
void clearToast() {
  if (toastUntil) { toastUntil = 0; menuHint(); }
}

void menuOpen(uint8_t id, int ctx, int cursor) {
  unsigned long t0 = millis();
  clearToast();
  depth++;
  st[depth] = {id, ctx, cursor, -1, ""};
  requestPage();
  menuHeader();
  menuRows();
  showMs(millis() - t0);
}

void menuExit(int newView) {
  unsigned long t0 = millis();
  inMenu = false;
  toastUntil = 0;
  if (newView >= 0) view = newView;
  for (int i = 0; i < ROWS; i++)                  // menu rows: only what was drawn
    if (rowW[i] > 0) tft.fillRect(0, ROW_Y0 + i * ROW_H, rowW[i], ROW_H, BLACK);
  tft.fillRect(ROW_W, ROW_Y0, 320 - ROW_W, ROWS * ROW_H, BLACK);   // scrollbar
  tft.fillRect(0, 204, 276, 22, BLACK);            // menu hint
  drawViewFull();
  showMs(millis() - t0);
}

void menuBack() {
  if (depth == 0) { menuExit(-1); return; }
  unsigned long t0 = millis();
  clearToast();
  depth--;
  requestPage();                               // only one page is kept: fetch it again
  menuHeader();
  menuRows();
  showMs(millis() - t0);
}

void menuMove(int d) {
  Level &l = st[depth];
  int n = listCount(l);
  if (n <= 0) return;                          // still loading
  int c = imax(0, imin(n - 1, l.cursor + d));
  if (c == l.cursor) return;
  int old = l.cursor;
  l.cursor = c;
  if (pageTop(c) != pageTop(old)) {
    unsigned long t0 = millis();
    requestPage();
    menuRows();
    showMs(millis() - t0);
  } else {
    menuRow(old - pageTop(old));
    menuRow(c - pageTop(c));
  }
  char b[16];
  snprintf(b, sizeof(b), "%d/%d", c + 1, n);
  textR(220, 0, 100, HEAD_H, 3, 6, b, 2, MUTED, BLACK);
}

void menuSelect() {
  Level &l = st[depth];
  int i = l.cursor;
  switch (l.id) {
    case L_ROOT: {
      uint8_t id = ROOT_LISTS[i];
      if (!rootAvailable(i)) { toast(id == L_CD ? "No CD drive" : "No USB stick"); break; }
      menuOpen(id, 0, id == L_QUEUE && curSong >= 0 ? curSong : 0);
      break;
    }
    case L_ARTISTS: {
      int row = i - pageTop(i);
      if (!pageHave[row]) break;               // not loaded yet
      char name[40];
      snprintf(name, sizeof(name), "%s", pageText[row]);
      menuOpen(L_ARTIST_ALBUMS, i, 0);
      snprintf(st[depth].title, sizeof(st[depth].title), "%s", name);
      menuHeader();
      break;
    }
    default:                                   // queue, albums, radio: play it
      if (listCount(l) <= 0) break;
      Bridge.notify("sel", (int)l.id, l.ctx, i, 0);
      menuExit(V_NOW);
      break;
  }
}

void menuLong() {
  Level &l = st[depth];
  if (l.id == L_ROOT || l.id == L_QUEUE || listCount(l) <= 0) return;
  Bridge.notify("sel", (int)l.id, l.ctx, l.cursor, 1);
  toast("Added to queue");
}

// ---- Input handling --------------------------------------------------------
bool logicDown = false;
uint16_t pressesDone = 0, releasesDone = 0, k0Done = 0;
int holdCount = 0;               // hold actions fired during the current press

void onShort() { if (inMenu) menuSelect(); else sendCmd(CMD_TOGGLE); }
// n = 1 after HOLD_MS, 2 after another REPEAT_MS, ...
void onHold(int n) {
  if (inMenu) { if (n == 1) menuLong(); }
  else sendCmd(CMD_NEXT);
}
void onK0()    { if (inMenu) menuBack();   else menuEnter(); }

void handleInput() {
  noInterrupts();
  pushISR();                       // polling backup for the interrupts
  k0ISR();
  int16_t dn = detN, dp = detP;
  detN = detP = 0;
  uint16_t pc = pushPresses, rc = pushReleases, kc = k0Presses;
  uint32_t pAt = pushPressAt, rAt = pushReleaseAt;
  bool turned = turnedP;
  interrupts();

  bool any = dn || dp || pc != pressesDone || rc != releasesDone || kc != k0Done;
  if (any) lastInputAt = millis();
  if (idle) {                      // any input only wakes the screen
    if (any) {
      exitIdle();
      pressesDone = pc; releasesDone = rc; k0Done = kc;
      logicDown = pushDown;
      swallowRelease = pushDown;   // the press that woke it is not a click
    }
    return;
  }

  // Turns first, so press + turn + release done during a slow redraw
  // still counts as a turn, not as a click.
  if (dp) { if (inMenu) menuMove(dp * ROWS); else switchView(dp); }
  if (dn) {
    if (inMenu) menuMove(dn);
    else if (volKnown) {
      // Shown at once; MPD's own value comes back in the next status.
      volume = imax(0, imin(100, volume + dn * VOLUME_STEP));
      volLocalAt = millis();
      drawVolume();
      Bridge.notify("vol", (int)dn);
    }
  }

  while (true) {
    if (!logicDown && pressesDone != pc) {
      pressesDone++;
      logicDown = true;
      holdCount = 0;
    } else if (logicDown && releasesDone != rc) {
      releasesDone++;
      logicDown = false;
      if (swallowRelease) swallowRelease = false;
      else if (!turned && holdCount == 0 && rAt - pAt < HOLD_MS) onShort();
    } else break;
  }

  // Held down (and not turning): act while still held, then repeat.
  if (logicDown && pushDown && !turned && !swallowRelease &&
      millis() - pAt >= HOLD_MS + (unsigned long)holdCount * REPEAT_MS) {
    holdCount++;
    onHold(holdCount);
  }

  while (k0Done != kc) { k0Done++; onK0(); }
}

// ---- Setup / loop ----------------------------------------------------------
void setup() {
  pinMode(PIN_A, INPUT_PULLUP);
  pinMode(PIN_B, INPUT_PULLUP);
  pinMode(PIN_PUSH, INPUT_PULLUP);
  pinMode(PIN_K0, INPUT_PULLUP);
  encState = (digitalRead(PIN_A) << 1) | digitalRead(PIN_B);
  encRest = encState;    // the knob is in a detent at power-up
  attachInterrupt(digitalPinToInterrupt(PIN_A), encoderISR, CHANGE);
  attachInterrupt(digitalPinToInterrupt(PIN_B), encoderISR, CHANGE);
  if (BUTTON_IRQ) {
    attachInterrupt(digitalPinToInterrupt(PIN_PUSH), pushISR, CHANGE);
    attachInterrupt(digitalPinToInterrupt(PIN_K0), k0ISR, CHANGE);
  }

  matrix.begin();
  matrix.setGrayscaleBits(3);   // 3-bit grayscale: brightness 0..7
  matrix.clear();

  tft.begin();
  tft.initBacklight(TFT_BLK);
  tft.setBacklight(255);
  tft.begin16(SPI16, 16000000);
  tft.setRotation(1);
  tft.fillScreen(BLACK);

  snprintf(now.src, sizeof(now.src), "MPD");
  drawViewFull();

  Bridge.begin();
  Bridge.provide("draw", draw);
  Bridge.provide("trk", onTrk);
  Bridge.provide("st", onSt);
  Bridge.provide("lv", onLv);
  Bridge.provide("clk", onClk);
  Bridge.provide("dev", onDev);
  Bridge.provide("page", onPage);
  Bridge.provide("item", onItem);
  Bridge.notify("hello");       // Python answers with the full state
  pausedSince = millis();
}

void loop() {
  handleInput();

  applyInbox();

  // Time runs on locally between status messages.
  unsigned long t = millis();
  if (playing && t - tickAt >= 1000) {
    tickAt += 1000;
    if (now.dur == 0 || elapsed < now.dur) elapsed++;
    onTimeTick();
  }

  if (idle) {
    if (t - rainFrameAt >= 40) { rainFrameAt = t; rainFrame(); }
    return;
  }
  if (!playing && t - pausedSince >= IDLE_MS && t - lastInputAt >= IDLE_MS) { enterIdle(); return; }

  if (!inMenu && clockNow() != clockShown) headerClock();
  if (!inMenu) marqueeStep();

  if (inMenu && toastUntil && t > toastUntil) { toastUntil = 0; menuHint(); }

  if (!inMenu && view != V_NOW && t - lastFrame >= FRAME_MS) {
    lastFrame = t;
    takeLevels();
    if (view == V_SPECTRUM) specFrame();
    else if (view == V_VU) vuFrame();
    else eyeFrame();
  }
}
