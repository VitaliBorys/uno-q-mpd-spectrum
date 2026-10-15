// MPD spectrum analyzer - MCU side.
// Receives 104-byte grayscale frames (8 rows x 13 columns, row-major,
// top row first, brightness 0..7) from Python over the Router Bridge
// and draws them on the UNO Q LED matrix.

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>

Arduino_LED_Matrix matrix;

// Bridge callback: draw one frame, ignore anything with the wrong size.
void draw(std::vector<uint8_t> frame) {
  if (frame.size() == 104) {
    matrix.draw(frame.data());
  }
}

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);   // 3-bit grayscale: brightness 0..7
  matrix.clear();

  Bridge.begin();
  Bridge.provide("draw", draw);
}

void loop() {
  // Nothing to do here: all work happens in the Bridge callback.
}
