# Nothing to do on the Linux side: the test runs on the MCU and shows its
# results on the TFT.
import time
from arduino.app_utils import App


def loop():
    time.sleep(1)


App.run(user_loop=loop)
