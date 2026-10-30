// SPI speed test for the 2.4" ST7789 on the UNO Q.
//
// Fills the whole screen (320x240x2 = 153600 bytes) several ways and shows
// how long each took:
//   LIB    the DIYables library, 8-bit SPI through Zephyr's driver
//   Z16    one Zephyr spi_write() per 8 KB chunk, 16-bit words (what the
//          spectrum sketch does with SPI16 = true)
//   RAW    the CPU writes SPI2's data register itself, skipping the driver,
//          at Zephyr's clock and at faster dividers
//
// Each fill draws colour bars: a broken transfer shows as garbage or
// shifted bars. The results stay on the screen; press the knob (D4) to
// run again.
//
// SPI2 (D11 MOSI, D13 SCK) of the STM32U585 is the "SPI v2" block (as on
// STM32H7). Zephyr configures it (clock, pins, mode); this sketch only
// switches it to transmit-only for the raw fills and restores it after.

#include <DIYables_TFT_SPI.h>
#include <SPI.h>

#define TFT_CS   10
#define TFT_DC    8
#define TFT_RST   9
#define TFT_BLK   6
#define PIN_PUSH  4

const uint32_t LIB_FREQ = 16000000;          // what the spectrum sketch asks for

// ---- SPI2 registers (STM32U5 reference manual, SPI chapter) ---------------
const uint32_t SPI2_BASE = 0x40003800;
#define REG(off) (*(volatile uint32_t *)(SPI2_BASE + (off)))
#define SPI_CR1   REG(0x00)
#define SPI_CR2   REG(0x04)
#define SPI_CFG1  REG(0x08)
#define SPI_CFG2  REG(0x0C)
#define SPI_IER   REG(0x10)
#define SPI_SR    REG(0x14)
#define SPI_IFCR  REG(0x18)
#define SPI_TXDR8  (*(volatile uint8_t *)(SPI2_BASE + 0x20))
#define SPI_TXDR16 (*(volatile uint16_t *)(SPI2_BASE + 0x20))

const uint32_t CR1_SPE = 1u << 0, CR1_CSTART = 1u << 9;
const uint32_t SR_TXP = 1u << 1, SR_TXC = 1u << 12;
const uint32_t IFCR_ALL = 0x0BF8;          // EOT, TXTF, UDR, OVR, CRCE, TIFRE, MODF, SUSP

class BenchTFT : public DIYables_ST7789_SPI {
public:
  using DIYables_ST7789_SPI::DIYables_ST7789_SPI;
  void window() {                       // whole screen, then RAMWR
    uint8_t b[4] = {0, 0, 319 >> 8, 319 & 0xFF};
    writeCommand(0x2A); spiWriteBlock(b, 4);
    uint8_t c[4] = {0, 0, 239 >> 8, 239 & 0xFF};
    writeCommand(0x2B); spiWriteBlock(c, 4);
    writeCommand(0x2C);                 // leaves DC high: data follows
  }
};

BenchTFT tft(240, 320, TFT_CS, TFT_DC, TFT_RST);

const int W = 320, H = 240;
const int CHUNK = 4096;                 // pixels per buffer (8 KB)
uint16_t buf[CHUNK];

// Colour bars, a different set for every test so a stale screen is obvious.
uint16_t barColor(int x, int set) {
  static const uint16_t c[8] = {0xF800, 0x07E0, 0x001F, 0xFFE0, 0x07FF, 0xF81F, 0xFFFF, 0x7BEF};
  return c[(x * 8 / W + set) % 8];
}
void fillBuf(int startPixel, int n, int set) {
  for (int i = 0; i < n; i++) buf[i] = barColor((startPixel + i) % W, set);
}

// ---- The three ways to fill the screen ------------------------------------
void fillLib(int set) {
  tft.startWrite();
  tft.window();
  uint8_t *p = (uint8_t *)buf;
  for (int done = 0; done < W * H; done += CHUNK) {
    int n = min(CHUNK, W * H - done);
    fillBuf(done, n, set);
    for (int i = 0; i < n; i++) { uint16_t v = buf[i]; p[2 * i] = v >> 8; p[2 * i + 1] = v; }
    SPI.transfer(p, n * 2);             // what the library's spiWriteBlock does
  }
  tft.endWrite();
}

struct SpiPeek : public arduino::ZephyrSPI {
  static const struct device *dev(arduino::ZephyrSPI &s) { return static_cast<SpiPeek &>(s).spi_dev; }
};

void fillZ16(int set) {
  struct spi_config cfg = {};
  cfg.frequency = LIB_FREQ;
  cfg.operation = SPI_OP_MODE_MASTER | SPI_TRANSFER_MSB | SPI_WORD_SET(16) | SPI_HOLD_ON_CS;
  const struct device *dev = SpiPeek::dev(SPI);
  tft.startWrite();
  tft.window();
  for (int done = 0; done < W * H; done += CHUNK) {
    int n = min(CHUNK, W * H - done);
    fillBuf(done, n, set);
    struct spi_buf b = {.buf = buf, .len = (size_t)n * 2};
    struct spi_buf_set s = {.buffers = &b, .count = 1};
    spi_write(dev, &cfg, &s);
  }
  tft.endWrite();
}

// mbr < 0: keep Zephyr's divider. bits: 8 or 16.
uint32_t lastMbr = 0;
void fillRaw(int set, int bits, int mbr) {
  tft.startWrite();                     // Zephyr sets up SPI2 for LIB_FREQ
  tft.window();                         // CS low, DC high
  uint32_t cfg1 = SPI_CFG1, cfg2 = SPI_CFG2, ier = SPI_IER;
  SPI_CR1 &= ~CR1_SPE;                  // config registers only change while off
  SPI_IER = 0;                          // keep Zephyr's interrupt handler out
  uint32_t c1 = cfg1 & ~(0x1Fu | (0xFu << 5));                 // DSIZE, FTHLV
  c1 |= (uint32_t)(bits - 1);
  if (mbr >= 0) c1 = (c1 & ~(7u << 28)) | ((uint32_t)mbr << 28);
  lastMbr = (c1 >> 28) & 7;
  SPI_CFG1 = c1;
  SPI_CFG2 = (cfg2 & ~(3u << 17)) | (1u << 17);                // COMM = transmit only
  SPI_CR2 = 0;                          // TSIZE = 0: no fixed length
  SPI_IFCR = IFCR_ALL;
  SPI_CR1 |= CR1_SPE;
  SPI_CR1 |= CR1_CSTART;
  for (int done = 0; done < W * H; done += CHUNK) {
    int n = min(CHUNK, W * H - done);
    fillBuf(done, n, set);
    if (bits == 16) {
      for (int i = 0; i < n; i++) { while (!(SPI_SR & SR_TXP)) {} SPI_TXDR16 = buf[i]; }
    } else {
      for (int i = 0; i < n; i++) {
        uint16_t v = buf[i];
        while (!(SPI_SR & SR_TXP)) {} SPI_TXDR8 = v >> 8;
        while (!(SPI_SR & SR_TXP)) {} SPI_TXDR8 = v & 0xFF;
      }
    }
  }
  uint32_t t0 = micros();
  while (!(SPI_SR & SR_TXC) && micros() - t0 < 10000) {}       // last bits out
  SPI_CR1 &= ~CR1_SPE;
  SPI_IFCR = IFCR_ALL;
  SPI_CFG1 = cfg1;                      // give SPI2 back to Zephyr as it was
  SPI_CFG2 = cfg2;
  SPI_IER = ier;
  tft.endWrite();
}

// ---- Running the tests ----------------------------------------------------
struct Result { const char *name; uint32_t us; int mbr; };
Result res[8];
int nres = 0;

uint32_t timeIt(void (*fn)(int), int set) { uint32_t t = micros(); fn(set); return micros() - t; }

void add(const char *name, uint32_t us, int mbr) { res[nres++] = {name, us, mbr}; delay(700); }

void runAll() {
  nres = 0;
  add("LIB  8-bit", timeIt(fillLib, 0), -1);
  add("Z16 16-bit", timeIt(fillZ16, 1), -1);
  uint32_t t;
  t = micros(); fillRaw(2, 8, -1);  add("RAW  8-bit", micros() - t, lastMbr);
  t = micros(); fillRaw(3, 16, -1); add("RAW 16-bit", micros() - t, lastMbr);
  t = micros(); fillRaw(4, 16, 2);  add("RAW 16 /8", micros() - t, lastMbr);
  t = micros(); fillRaw(5, 16, 1);  add("RAW 16 /4", micros() - t, lastMbr);

  tft.fillScreen(0);
  tft.setTextSize(2);
  tft.setTextColor(DIYables_TFT_SPI::colorRGB(0, 255, 120), 0);
  tft.setCursor(4, 4);
  tft.print("FULL SCREEN 153600 B");
  for (int i = 0; i < nres; i++) {
    char line[48];
    uint32_t kbs = (uint32_t)(153600ULL * 1000 / (res[i].us ? res[i].us : 1));   // KB/s (1 KB = 1000 B)
    if (res[i].mbr >= 0)
      snprintf(line, sizeof(line), "%-10s%5lu%6lu  /%d", res[i].name,
               (unsigned long)(res[i].us / 1000), (unsigned long)kbs, 2 << res[i].mbr);
    else
      snprintf(line, sizeof(line), "%-10s%5lu%6lu", res[i].name,
               (unsigned long)(res[i].us / 1000), (unsigned long)kbs);
    tft.setCursor(4, 34 + i * 26);
    tft.print(line);
  }
  tft.setTextColor(DIYables_TFT_SPI::colorRGB(0, 153, 72), 0);
  tft.setCursor(4, 216);
  tft.print("          ms  KB/s  clk");
}

void setup() {
  pinMode(PIN_PUSH, INPUT_PULLUP);
  tft.begin(LIB_FREQ);
  tft.initBacklight(TFT_BLK);
  tft.setBacklight(255);
  tft.setRotation(1);
  tft.fillScreen(0);
  delay(300);
  runAll();
}

void loop() {
  if (digitalRead(PIN_PUSH) == LOW) {
    delay(30);
    while (digitalRead(PIN_PUSH) == LOW) {}
    runAll();
  }
}
