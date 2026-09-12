from enum import StrEnum

class RemoteKey(StrEnum):
    DPAD_UP = "DPAD_UP"
    DPAD_DOWN = "DPAD_DOWN"
    DPAD_LEFT = "DPAD_LEFT"
    DPAD_RIGHT = "DPAD_RIGHT"
    DPAD_CENTER = "DPAD_CENTER"
    BACK = "BACK"
    HOME = "HOME"
    MENU = "MENU"
    POWER = "POWER"
    VOLUME_UP = "VOLUME_UP"
    VOLUME_DOWN = "VOLUME_DOWN"
    MUTE = "MUTE"
    PLAY = "PLAY"
    PAUSE = "PAUSE"
    STOP = "STOP"
    NEXT = "NEXT"
    PREVIOUS = "PREVIOUS"
    TAB = "TAB"
    ENTER = "ENTER"
    DEL = "DEL"
    DIGIT_0 = "DIGIT_0"
    DIGIT_1 = "DIGIT_1"
    DIGIT_2 = "DIGIT_2"
    DIGIT_3 = "DIGIT_3"
    DIGIT_4 = "DIGIT_4"
    DIGIT_5 = "DIGIT_5"
    DIGIT_6 = "DIGIT_6"
    DIGIT_7 = "DIGIT_7"
    DIGIT_8 = "DIGIT_8"
    DIGIT_9 = "DIGIT_9"

_KEY_CODES: dict[RemoteKey, str] = {
    RemoteKey.DPAD_UP: "19",
    RemoteKey.DPAD_DOWN: "20",
    RemoteKey.DPAD_LEFT: "21",
    RemoteKey.DPAD_RIGHT: "22",
    RemoteKey.DPAD_CENTER: "23",
    RemoteKey.BACK: "4",
    RemoteKey.HOME: "3",
    RemoteKey.MENU: "82",
    RemoteKey.POWER: "26",
    RemoteKey.VOLUME_UP: "24",
    RemoteKey.VOLUME_DOWN: "25",
    RemoteKey.MUTE: "164",
    RemoteKey.PLAY: "126",
    RemoteKey.PAUSE: "127",
    RemoteKey.STOP: "86",
    RemoteKey.NEXT: "87",
    RemoteKey.PREVIOUS: "88",
    RemoteKey.TAB: "61",
    RemoteKey.ENTER: "66",
    RemoteKey.DEL: "67",
    **{RemoteKey[f"DIGIT_{i}"]: str(7 + i) for i in range(10)},
}



