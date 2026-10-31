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

// ---- SPI2 registers --------------------------------------------------------
// The core already pulls in ST's CMSIS header for the STM32U585, so SPI2 is
// the register block (SPI_TypeDef) and the bit names come from there too.
#define SPI_TXDR8  (*(volatile uint8_t *)&SPI2->TXDR)
#define SPI_TXDR16 (*(volatile uint16_t *)&SPI2->TXDR)
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
  uint32_t cfg1 = SPI2->CFG1, cfg2 = SPI2->CFG2, ier = SPI2->IER;
  SPI2->CR1 &= ~SPI_CR1_SPE;            // config registers only change while off
  SPI2->IER = 0;                        // keep Zephyr's interrupt handler out
  uint32_t c1 = cfg1 & ~(SPI_CFG1_DSIZE | SPI_CFG1_FTHLV);
  c1 |= (uint32_t)(bits - 1);
  if (mbr >= 0) c1 = (c1 & ~SPI_CFG1_MBR) | ((uint32_t)mbr << SPI_CFG1_MBR_Pos);
  lastMbr = (c1 & SPI_CFG1_MBR) >> SPI_CFG1_MBR_Pos;
  SPI2->CFG1 = c1;
  SPI2->CFG2 = (cfg2 & ~SPI_CFG2_COMM) | SPI_CFG2_COMM_0;      // transmit only
  SPI2->CR2 = 0;                        // TSIZE = 0: no fixed length
  SPI2->IFCR = IFCR_ALL;
  SPI2->CR1 |= SPI_CR1_SPE;
  SPI2->CR1 |= SPI_CR1_CSTART;
  for (int done = 0; done < W * H; done += CHUNK) {
    int n = min(CHUNK, W * H - done);
    fillBuf(done, n, set);
    if (bits == 16) {
      for (int i = 0; i < n; i++) { while (!(SPI2->SR & SPI_SR_TXP)) {} SPI_TXDR16 = buf[i]; }
    } else {
      for (int i = 0; i < n; i++) {
        uint16_t v = buf[i];
        while (!(SPI2->SR & SPI_SR_TXP)) {} SPI_TXDR8 = v >> 8;
        while (!(SPI2->SR & SPI_SR_TXP)) {} SPI_TXDR8 = v & 0xFF;
      }
    }
  }
  uint32_t t0 = micros();
  while (!(SPI2->SR & SPI_SR_TXC) && micros() - t0 < 10000) {}       // last bits out
  SPI2->CR1 &= ~SPI_CR1_SPE;
  SPI2->IFCR = IFCR_ALL;
  SPI2->CFG1 = cfg1;                      // give SPI2 back to Zephyr as it was
  SPI2->CFG2 = cfg2;
  SPI2->IER = ier;
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
