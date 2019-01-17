# -*- coding: utf-8 -*-
# @Author: Niccolò Bonacchi
# @Date:   2018-02-02 17:19:09
# @Last Modified by:   Niccolò Bonacchi
# @Last Modified time: 2018-07-12 16:18:59
import json
import os
import shutil
import subprocess
import time
import zipfile
import sys
from sys import platform
from pathlib import Path
import logging

import numpy as np
import pandas as pd
import scipy as sp
import scipy.interpolate as interp
from pybpod_rotaryencoder_module.module_api import RotaryEncoderModule
from pythonosc import udp_client

import ibllib.io.raw_data_loaders as raw
import sound
sys.path.append(str(Path(__file__).parent.parent))
sys.path.append(str(Path(__file__).parent.parent.parent.parent))
from path_helper import SessionPathCreator
import init_logging
logger = logging.getLogger('iblrig')


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if hasattr(obj, 'reprJSON'):
            return obj.reprJSON()
        else:
            return json.JSONEncoder.default(self, obj)


class MyRotaryEncoder(object):

    def __init__(self, all_thresholds, gain):
        self.all_thresholds = all_thresholds
        self.wheel_perim = 31 * 2 * np.pi  # = 194,778744523
        self.deg_mm = 360 / self.wheel_perim
        self.mm_deg = self.wheel_perim / 360
        self.factor = 1 / (self.mm_deg * gain)
        self.SET_THRESHOLDS = [x * self.factor for x in self.all_thresholds]
        self.ENABLE_THRESHOLDS = [(True if x != 0
                                   else False) for x in self.SET_THRESHOLDS]
        # ENABLE_THRESHOLDS needs 8 bools even if only 2 thresholds are set
        while len(self.ENABLE_THRESHOLDS) < 8:
            self.ENABLE_THRESHOLDS.append(False)

    def reprJSON(self):
        d = self.__dict__
        return d


class SessionParamHandler(object):
    """Session object imports user_settings and task_settings
    will and calculates other secondary session parameters,
    runs Bonsai and saves all params in a settings file.json"""

    def __init__(self, task_settings, user_settings, debug=False, fmake=True):
        self.DEBUG = debug
        make = False if not fmake else ['video']
        # =====================================================================
        # IMPORT task_settings, user_settings, and SessionPathCreator params
        # =====================================================================
        ts = {i: task_settings.__dict__[i]
              for i in [x for x in dir(task_settings) if '__' not in x]}
        self.__dict__.update(ts)
        us = {i: user_settings.__dict__[i]
              for i in [x for x in dir(user_settings) if '__' not in x]}
        self.__dict__.update(us)
        self.deserialize_session_user_settings()
        spc = SessionPathCreator(self.IBLRIG_FOLDER, self.IBLRIG_DATA_FOLDER,
                                 self.PYBPOD_SUBJECTS[0],
                                 protocol=self.PYBPOD_PROTOCOL,
                                 board=self.PYBPOD_BOARD, make=make)
        self.__dict__.update(spc.__dict__)
        self._check_com_config()

        if self.INTERACTIVE_DELAY < 0.1:
            self.INTERACTIVE_DELAY = 0.1
        # =====================================================================
        # SUBJECT
        # =====================================================================
        self.SUBJECT_WEIGHT = self.get_subject_weight()
        # =====================================================================
        # OSC CLIENT
        # =====================================================================
        self.OSC_CLIENT_PORT = 7110
        self.OSC_CLIENT_IP = '127.0.0.1'
        self.OSC_CLIENT = self._init_osc_client()
        # =====================================================================
        # PREVIOUS DATA FILES
        # =====================================================================
        self.LAST_TRIAL_DATA = self._load_last_trial()
        self.LAST_SETTINGS_DATA = self._load_last_settings_file()
        # =====================================================================
        # ADAPTIVE STUFF
        # =====================================================================
        self.REWARD_AMOUNT = self._init_reward_amount()
        self.CALIB_FUNC = self._init_calib_func()
        self.REWARD_VALVE_TIME = self._init_reward_valve_time()

        self.STIM_GAIN = self._init_stim_gain()
        # =====================================================================
        # ROTARY ENCODER
        # =====================================================================
        self.ALL_THRESHOLDS = (self.STIM_POSITIONS +
                               self.QUIESCENCE_THRESHOLDS)
        self.ROTARY_ENCODER = MyRotaryEncoder(self.ALL_THRESHOLDS,
                                              self.STIM_GAIN)
        # Names of the RE events generated by Bpod
        self.ENCODER_EVENTS = ['RotaryEncoder1_{}'.format(x) for x in
                               list(range(1, len(self.ALL_THRESHOLDS) + 1))]
        # Dict mapping threshold crossings with name ov RE event
        self.THRESHOLD_EVENTS = dict(zip(self.ALL_THRESHOLDS,
                                         self.ENCODER_EVENTS))
        if not self.DEBUG:
            self._configure_rotary_encoder(RotaryEncoderModule)
        # =====================================================================
        # SOUNDS
        # =====================================================================
        if self.SOFT_SOUND == 'xonar':
            self.SOUND_SAMPLE_FREQ = 192000
        elif self.SOFT_SOUND == 'sysdefault':
            self.SOUND_SAMPLE_FREQ = 44100
        elif self.SOFT_SOUND is None:
            self.SOUND_SAMPLE_FREQ = 96000

        self.WHITE_NOISE_DURATION = float(self.WHITE_NOISE_DURATION)
        self.WHITE_NOISE_AMPLITUDE = float(self.WHITE_NOISE_AMPLITUDE)
        self.GO_TONE_DURATION = float(self.GO_TONE_DURATION)
        self.GO_TONE_FREQUENCY = int(self.GO_TONE_FREQUENCY)
        self.GO_TONE_AMPLITUDE = float(self.GO_TONE_AMPLITUDE)

        self.SD = sound.configure_sounddevice(
            output=self.SOFT_SOUND, samplerate=self.SOUND_SAMPLE_FREQ)

        self._init_sounds()  # Will create sounds and output actions.
        # =====================================================================
        # RUN VISUAL STIM
        # =====================================================================
        self.BONSAI = spc.get_bonsai_path(use_iblrig_bonsai=True)
        self.VISUAL_STIMULUS_TYPE = 'TrainingGabor2D'
        self.VISUAL_STIMULUS_FILE = str(
            Path(self.VISUAL_STIM_FOLDER) /
            self.VISUAL_STIMULUS_TYPE / 'Gabor2D.bonsai')
        self.start_visual_stim()
        # =====================================================================
        # SAVE SETTINGS FILE AND TASK CODE
        # =====================================================================
        if not self.DEBUG:
            self._save_session_settings()

            self._copy_task_code()
            self._save_task_code()
            self.bpod_lights(0)

    def _check_com_config(self):
        comports = {'BPOD': self.COM['BPOD'], 'ROTARY_ENCODER': None,
                    'FRAME2TTL': None}
        logger.debug(f"COMPORTS: {str(self.COM)}")
        if not self.COM['ROTARY_ENCODER']:
            comports['ROTARY_ENCODER'] = self.strinput(
                "RIG CONFIG",
                "Please insert ROTARY ENCODER COM port (e.g. COM9): ").upper()
            logger.debug(
                f"Updating comport file with ROTARY_ENCODER port {comports['ROTARY_ENCODER']}")
            SessionPathCreator.create_bpod_comport_file(
                self.BPOD_COMPORTS_FILE, comports)
            self.COM = comports
        if not self.COM['FRAME2TTL']:
            comports['FRAME2TTL'] = self.strinput(
                "RIG CONFIG",
                "Please insert FRAME2TTL COM port (e.g. COM9): ").upper()
            logger.debug(
                f"Updating comport file with FRAME2TTL port {comports['FRAME2TTL']}")
            SessionPathCreator.create_bpod_comport_file(
                self.BPOD_COMPORTS_FILE, comports)
            self.COM = comports

    # =========================================================================
    # STATIC METHODS
    # =========================================================================
    @staticmethod
    def numinput(title, prompt, default=None, minval=None, maxval=None):
        """ From turtle lib:
        Pop up a dialog window for input of a number.
        Arguments:
        title: is the title of the dialog window,
        prompt: is a text describing what numerical information to input.
        default: default value
        minval: minimum value for imput
        maxval: maximum value for input

        The number input must be in the range minval .. maxval if these are
        given. If not, a hint is issued and the dialog remains open for
        correction. Return the number input.
        If the dialog is canceled,  return None.

        Example:
        >>> numinput("Poker", "Your stakes:", 1000, minval=10, maxval=10000)
        """
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        return simpledialog.askfloat(title, prompt, initialvalue=default,
                                     minvalue=minval, maxvalue=maxval)

    @staticmethod
    def strinput(title, prompt, default='COM'):
        """
        Example:
        >>> strinput("RIG CONFIG", "Insert RE com port:", default="COM")
        """
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        return simpledialog.askstring(title, prompt, initialvalue=default)

    @staticmethod
    def zipdir(path, ziph):
        # ziph is zipfile handle
        for root, dirs, files in os.walk(path):
            for file in files:
                ziph.write(os.path.join(root, file),
                           os.path.relpath(os.path.join(root, file),
                                           os.path.join(path, '..')))

    @staticmethod
    def zipit(dir_list, zip_name):
        zipf = zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED)
        for dir in dir_list:
            SessionParamHandler.zipdir(dir, zipf)
        zipf.close()

    # =========================================================================
    # METHODS
    # =========================================================================
    def get_subject_weight(self):
        return self.numinput(
            "Subject weighing (gr)", f"{self.PYBPOD_SUBJECTS[0]} weight (gr):")

    def bpod_lights(self, command: int):
        fpath = Path(self.IBLRIG_PARAMS_FOLDER) / 'bpod_lights.py'
        os.system(f"python {fpath} {command}")

    # =========================================================================
    # SERIALIZER
    # =========================================================================
    def reprJSON(self):
        def remove_from_dict(sx):
            if "weighings" in sx.keys():
                sx["weighings"] = None
            if "water_administration" in sx.keys():
                sx["water_administration"] = None
            return sx

        d = self.__dict__.copy()
        if self.SOFT_SOUND:
            d['GO_TONE'] = 'go_tone(freq={}, dur={}, amp={})'.format(
                self.GO_TONE_FREQUENCY, self.GO_TONE_DURATION,
                self.GO_TONE_AMPLITUDE)
            d['WHITE_NOISE'] = 'white_noise(freq=-1, dur={}, amp={})'.format(
                self.WHITE_NOISE_DURATION, self.WHITE_NOISE_AMPLITUDE)
        d['SD'] = str(d['SD'])
        d['OSC_CLIENT'] = str(d['OSC_CLIENT'])
        d['SESSION_DATETIME'] = self.SESSION_DATETIME.isoformat()
        d['CALIB_FUNC'] = str(d['CALIB_FUNC'])
        if isinstance(d['PYBPOD_SUBJECT_EXTRA'], list):
            sub = []
            for sx in d['PYBPOD_SUBJECT_EXTRA']:
                sub.append(remove_from_dict(sx))
            d['PYBPOD_SUBJECT_EXTRA'] = sub
        elif isinstance(d['PYBPOD_SUBJECT_EXTRA'], dict):
            d['PYBPOD_SUBJECT_EXTRA'] = remove_from_dict(
                d['PYBPOD_SUBJECT_EXTRA'])
        d['LAST_TRIAL_DATA'] = None
        d['LAST_SETTINGS_DATA'] = None

        return d

    # =========================================================================
    # SOUND
    # =========================================================================
    def _init_sounds(self):
        if self.SOFT_SOUND:
            self.UPLOADER_TOOL = None
            self.GO_TONE = sound.make_sound(
                rate=self.SOUND_SAMPLE_FREQ,
                frequency=self.GO_TONE_FREQUENCY,
                duration=self.GO_TONE_DURATION,
                amplitude=self.GO_TONE_AMPLITUDE,
                fade=0.01,
                chans='L+TTL')
            self.WHITE_NOISE = sound.make_sound(
                rate=self.SOUND_SAMPLE_FREQ,
                frequency=-1,
                duration=self.WHITE_NOISE_DURATION,
                amplitude=self.WHITE_NOISE_AMPLITUDE,
                fade=0.01,
                chans='L+TTL')

            self.OUT_TONE = ('SoftCode', 1)
            self.OUT_NOISE = ('SoftCode', 2)
        else:
            print("\n\nSOUND BOARD NOT IMPLEMTNED YET!!",
                  "\nPLEASE USE SOFT_SOUND =",
                  "'sysdefault' or 'xonar' in task_settings.py\n\n")

    def play_tone(self):
        self.SD.play(self.GO_TONE, self.SOUND_SAMPLE_FREQ)  # , mapping=[1, 2])

    def play_noise(self):
        self.SD.play(self.WHITE_NOISE, self.SOUND_SAMPLE_FREQ)

    def stop_sound(self):
        self.SD.stop()

    # =========================================================================
    # BONSAI WORKFLOWS
    # =========================================================================
    def start_visual_stim(self):
        if self.USE_VISUAL_STIMULUS and self.BONSAI:
            # Run Bonsai workflow
            here = os.getcwd()
            os.chdir(str(
                Path(self.VISUAL_STIM_FOLDER) / self.VISUAL_STIMULUS_TYPE))
            bns = self.BONSAI
            wkfl = self.VISUAL_STIMULUS_FILE

            evt = "-p:FileNameEvents=" + os.path.join(
                self.SESSION_RAW_DATA_FOLDER,
                "_iblrig_encoderEvents.raw.ssv")
            pos = "-p:FileNamePositions=" + os.path.join(
                self.SESSION_RAW_DATA_FOLDER,
                "_iblrig_encoderPositions.raw.ssv")
            itr = "-p:FileNameTrialInfo=" + os.path.join(
                self.SESSION_RAW_DATA_FOLDER,
                "_iblrig_encoderTrialInfo.raw.ssv")
            mic = "-p:FileNameMic=" + os.path.join(
                self.SESSION_RAW_DATA_FOLDER,
                "_iblrig_micData.raw.wav")

            com = "-p:REPortName=" + self.COM['ROTARY_ENCODER']
            rec = "-p:RecordSound=" + str(self.RECORD_SOUND)

            start = '--start'
            noeditor = '--noeditor'

            if self.BONSAI_EDITOR:
                subprocess.Popen(
                    [bns, wkfl, start, pos, evt, itr, com, mic, rec])
            elif not self.BONSAI_EDITOR:
                subprocess.Popen(
                    [bns, wkfl, noeditor, pos, evt, itr, com, mic, rec])
            time.sleep(5)
            os.chdir(here)
        else:
            self.USE_VISUAL_STIMULUS = False

    def start_camera_recording(self):
        if (self.RECORD_VIDEO is False
                and self.OPEN_CAMERA_VIEW is False):
            return
        # Run Workflow
        here = os.getcwd()
        os.chdir(self.VIDEO_RECORDING_FOLDER)

        bns = self.BONSAI
        wkfl = self.VIDEO_RECORDING_FILE

        ts = '-p:TimestampsFileName=' + os.path.join(
            self.SESSION_RAW_VIDEO_DATA_FOLDER,
            '_iblrig_leftCamera.timestamps.ssv')
        vid = '-p:VideoFileName=' + os.path.join(
            self.SESSION_RAW_VIDEO_DATA_FOLDER,
            '_iblrig_leftCamera.raw.avi')
        rec = '-p:SaveVideo=' + str(self.RECORD_VIDEO)

        start = '--start'

        subprocess.Popen([bns, wkfl, start, ts, vid, rec])
        time.sleep(1)
        os.chdir(here)

    # =========================================================================
    # LAST TRIAL DATA
    # =========================================================================
    def _load_last_trial(self, i=-1):
        if self.PREVIOUS_DATA_FILE is None:
            return
        trial_data = raw.load_data(self.PREVIOUS_SESSION_PATH)
        print("\n\nINFO: PREVIOUS SESSION FOUND",
              "\nLOADING PARAMETERS FROM: {}".format(self.PREVIOUS_DATA_FILE),
              "\n\nPREVIOUS NTRIALS:              {}".format(
                  trial_data[i]["trial_num"]),
              "\nPREVIOUS NTRIALS (no repeats): {}".format(
                  trial_data[i]["non_rc_ntrials"]),
              "\nLAST REWARD:                   {}".format(
                  trial_data[i]["reward_amount"]),
              "\nLAST GAIN:                     {}".format(
                  trial_data[i]["stim_gain"]),
              "\nLAST CONTRAST SET:             {}".format(
                  trial_data[i]["ac"]["contrast_set"]),
              "\nBUFFERS LR:                    {}".format(
                  trial_data[i]["ac"]["buffer"]))

        return trial_data[i] if trial_data else None

    def _load_last_settings_file(self):
        if not self.PREVIOUS_SETTINGS_FILE:
            return

        return raw.load_settings(self.PREVIOUS_SESSION_PATH)

    # =========================================================================
    # ADAPTIVE REWARD AND GAIN RULES
    # =========================================================================
    def _init_reward_amount(self):
        if not self.ADAPTIVE_REWARD:
            return self.REWARD_AMOUNT

        if self.LAST_TRIAL_DATA is None:
            return self.AR_INIT_VALUE
        elif self.LAST_TRIAL_DATA and self.LAST_TRIAL_DATA['trial_num'] < 200:
            out = self.LAST_TRIAL_DATA['reward_amount']
        elif self.LAST_TRIAL_DATA and self.LAST_TRIAL_DATA['trial_num'] >= 200:
            out = self.LAST_TRIAL_DATA['reward_amount'] - self.AR_STEP
            out = self.AR_MIN_VALUE if out <= self.AR_MIN_VALUE else out

        if 'SUBJECT_WEIGHT' not in self.LAST_SETTINGS_DATA.keys():
            return out

        previous_weight_factor = self.LAST_SETTINGS_DATA['SUBJECT_WEIGHT'] / 25
        previous_water = self.LAST_TRIAL_DATA['water_delivered'] / 1000

        if previous_water < previous_weight_factor:
            out = self.LAST_TRIAL_DATA['reward_amount'] + self.AR_STEP

        print(f"\nREWARD AMOUNT: {out}")
        print(f"PREVIOUS WEIGHT: {self.LAST_SETTINGS_DATA['SUBJECT_WEIGHT']}")
        print(
            f"PREVIOUS WATER DRANK: {self.LAST_TRIAL_DATA['water_delivered']}")

        return out

    def _init_calib_func(self):
        if not self.AUTOMATIC_CALIBRATION:
            return

        if self.LATEST_WATER_CALIBRATION_FILE:
            # Load last calibration df1
            df1 = pd.read_csv(self.LATEST_WATER_CALIBRATION_FILE)
            # make interp func
            if df1.empty:
                logger.error(f"Water calibration file is emtpy!")
                raise(ValueError)
            time2vol = sp.interpolate.pchip(df1["open_time"],
                                            df1["weight_perdrop"])
            return time2vol
        else:
            return

    def _init_reward_valve_time(self):
        # Calc reward valve time
        if not self.AUTOMATIC_CALIBRATION:
            out = self.CALIBRATION_VALUE / 3 * self.REWARD_AMOUNT
        elif self.AUTOMATIC_CALIBRATION and self.CALIB_FUNC is not None:
            out = 0
            while np.round(self.CALIB_FUNC(out), 3) < self.REWARD_AMOUNT:
                out += 1
            out /= 1000
        elif self.AUTOMATIC_CALIBRATION and self.CALIB_FUNC is None:
            print("\n\nNO CALIBRATION FILE WAS FOUND:",
                  "\nCalibrate the rig or use a manual calibration value.",
                  "\n\n")
            raise ValueError

        print("\n\nREWARD_VALVE_TIME:", out, "\n\n")
        if out >= 1:
            print("\n\nREWARD_VALVE_TIME is too high!:", out,
                  "\nProbably because of a BAD calibration file...",
                  "\nCalibrate the rig or use a manual calibration value.")
            raise(ValueError)

        return float(out)

    def _init_stim_gain(self):
        if not self.ADAPTIVE_GAIN:
            return self.STIM_GAIN

        if self.LAST_TRIAL_DATA and self.LAST_TRIAL_DATA['trial_num'] >= 200:
            stim_gain = self.AG_MIN_VALUE
        else:
            stim_gain = self.AG_INIT_VALUE

        return stim_gain

    # =========================================================================
    # OSC CLIENT
    # =========================================================================
    def _init_osc_client(self):
        osc_client = udp_client.SimpleUDPClient(self.OSC_CLIENT_IP,
                                                self.OSC_CLIENT_PORT)
        return osc_client

    # =========================================================================
    # PYBPOD USER SETTINGS DESERIALIZATION
    # =========================================================================
    def deserialize_session_user_settings(self):
        self.PYBPOD_CREATOR = json.loads(self.PYBPOD_CREATOR)
        self.PYBPOD_USER_EXTRA = json.loads(self.PYBPOD_USER_EXTRA)

        self.PYBPOD_SUBJECTS = [json.loads(x.replace("'", '"'))
                                for x in self.PYBPOD_SUBJECTS]
        if len(self.PYBPOD_SUBJECTS) == 1:
            self.PYBPOD_SUBJECTS = self.PYBPOD_SUBJECTS[0]
        else:
            print("ERROR: Multiple subjects found in PYBPOD_SUBJECTS")
            raise IOError

        self.PYBPOD_SUBJECT_EXTRA = [
          json.loads(x) for x in self.PYBPOD_SUBJECT_EXTRA[1:-1].split('","')]
        if len(self.PYBPOD_SUBJECT_EXTRA) == 1:
            self.PYBPOD_SUBJECT_EXTRA = self.PYBPOD_SUBJECT_EXTRA[0]

    # =========================================================================
    # SERIALIZE, COPY AND SAVE
    # =========================================================================
    def _save_session_settings(self):
        with open(self.SETTINGS_FILE_PATH, 'a') as f:
            f.write(json.dumps(self, cls=ComplexEncoder, indent=1))
            f.write('\n')
        return

    def _copy_task_code(self):
        # Copy behavioral task python code
        src = os.path.join(self.IBLRIG_PARAMS_FOLDER, 'IBL', 'tasks',
                           self.PYBPOD_PROTOCOL)
        dst = os.path.join(self.SESSION_RAW_DATA_FOLDER, self.PYBPOD_PROTOCOL)
        shutil.copytree(src, dst)
        # Copy stimulus folder with bonsai workflow
        src = str(Path(self.VISUAL_STIM_FOLDER) / self.VISUAL_STIMULUS_TYPE)
        dst = str(Path(self.SESSION_RAW_DATA_FOLDER) /
                  self.VISUAL_STIMULUS_TYPE)
        shutil.copytree(src, dst)
        # Copy video recording folder with bonsai workflow
        src = self.VIDEO_RECORDING_FOLDER
        dst = os.path.join(self.SESSION_RAW_VIDEO_DATA_FOLDER,
                           'camera_recordings')
        shutil.copytree(src, dst)

    def _save_task_code(self):
        # zip all existing folders
        # Should be the task code folder and if available stimulus code folder
        behavior_code_files = [
            os.path.join(self.SESSION_RAW_DATA_FOLDER, x)
            for x in os.listdir(self.SESSION_RAW_DATA_FOLDER)
            if os.path.isdir(os.path.join(self.SESSION_RAW_DATA_FOLDER, x))
        ]
        SessionParamHandler.zipit(
            behavior_code_files, os.path.join(self.SESSION_RAW_DATA_FOLDER,
                                              '_iblrig_taskCodeFiles.raw.zip'))

        video_code_files = [
            os.path.join(self.SESSION_RAW_VIDEO_DATA_FOLDER, x)
            for x in os.listdir(self.SESSION_RAW_VIDEO_DATA_FOLDER)
            if os.path.isdir(os.path.join(
                self.SESSION_RAW_VIDEO_DATA_FOLDER, x))]
        SessionParamHandler.zipit(
            video_code_files, os.path.join(self.SESSION_RAW_VIDEO_DATA_FOLDER,
                                           '_iblrig_videoCodeFiles.raw.zip'))

        [shutil.rmtree(x) for x in behavior_code_files + video_code_files]

    def _configure_rotary_encoder(self, RotaryEncoderModule):
        m = RotaryEncoderModule(self.COM['ROTARY_ENCODER'])
        m.set_zero_position()  # Not necessarily needed
        m.set_thresholds(self.ROTARY_ENCODER.SET_THRESHOLDS)
        m.enable_thresholds(self.ROTARY_ENCODER.ENABLE_THRESHOLDS)
        m.close()


if __name__ == '__main__':
    """
    SessionParamHandler fmake flag=False disables:
        making folders/files;
    SessionParamHandler debug flag disables:
        running auto calib;
        calling bonsai
        turning off lights of bpod board
    """
    import task_settings as _task_settings
    import scratch._user_settings as _user_settings
    if platform == 'linux':
        r = "/home/nico/Projects/IBL/IBL-github/iblrig"
        _task_settings.IBLRIG_FOLDER = r
        d = "/home/nico/Projects/IBL/IBL-github/iblrig/scratch/test_iblrig_data"
        _task_settings.IBLRIG_DATA_FOLDER = d
        _task_settings.AUTOMATIC_CALIBRATION = False
        _task_settings.USE_VISUAL_STIMULUS = False

    sph = SessionParamHandler(_task_settings, _user_settings,
                              debug=True, fmake=False)
    for k in sph.__dict__:
        print(f"{k}: {sph.__dict__[k]}")
    self = sph
    print("Done!")
