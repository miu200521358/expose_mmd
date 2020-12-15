# -*- coding: utf-8 -*-
import os
import argparse
import glob
import re
import json
import csv
import cv2
import shutil
import sys

# import vision essentials
import numpy as np
from tqdm import tqdm

from mmd.utils.MLogger import MLogger
from mmd.utils.MServiceUtils import sort_by_numeric
from mmd.mmd.VmdData import OneEuroFilter
from lighttrack.visualizer.detection_visualizer import draw_bbox


logger = MLogger(__name__, level=MLogger.DEBUG)

def execute(args):
    try:
        logger.info('関節スムージング処理開始: {0}', args.img_dir, decoration=MLogger.DECORATION_BOX)

        if not os.path.exists(args.img_dir):
            logger.error("指定された処理用ディレクトリが存在しません。: {0}", args.img_dir, decoration=MLogger.DECORATION_BOX)
            return False

        # 全人物分の順番別フォルダ
        ordered_person_dir_pathes = sorted(glob.glob(os.path.join(args.img_dir, "ordered", "*")), key=sort_by_numeric)

        frame_pattern = re.compile(r'^frame_(\d+)\.')

        for oidx, ordered_person_dir_path in enumerate(ordered_person_dir_pathes):    
            logger.info("【No.{0}】関節スムージング開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)

            frame_json_pathes = sorted(glob.glob(os.path.join(ordered_person_dir_path, "frame_*.json")), key=sort_by_numeric)

            all_joints = {}
        
            for frame_json_path in tqdm(frame_json_pathes, desc=f"Read No.{oidx:03} ... "):
                m = frame_pattern.match(os.path.basename(frame_json_path))
                if m:
                    # キーフレの場所を確定（間が空く場合もある）
                    fno = int(m.groups()[0])

                    frame_joints = {}
                    with open(frame_json_path, 'r') as f:
                        frame_joints = json.load(f)
                    
                    # ジョイントグローバル座標を保持
                    for jname, joint in frame_joints["joints"].items():
                        if (jname, 'x') not in all_joints:
                            all_joints[(jname, 'x')] = {}

                        if (jname, 'y') not in all_joints:
                            all_joints[(jname, 'y')] = {}

                        if (jname, 'z') not in all_joints:
                            all_joints[(jname, 'z')] = {}
                        
                        all_joints[(jname, 'x')][fno] = joint["x"]
                        all_joints[(jname, 'y')][fno] = joint["y"]
                        all_joints[(jname, 'z')][fno] = joint["z"]

                    if "faces" in frame_joints:
                        # 表情グローバル座標を保持
                        for fname, face in frame_joints["faces"].items():
                            if (fname, 'fx') not in all_joints:
                                all_joints[(fname, 'fx')] = {}

                            if (fname, 'fy') not in all_joints:
                                all_joints[(fname, 'fy')] = {}
                            
                            all_joints[(fname, 'fx')][fno] = face["x"]
                            all_joints[(fname, 'fy')][fno] = face["y"]
                    
                    if "eyes" in frame_joints:
                        for ename, eye in frame_joints["eyes"].items():
                            if (ename, 'ex') not in all_joints:
                                all_joints[(ename, 'ex')] = {}

                            if (ename, 'ey') not in all_joints:
                                all_joints[(ename, 'ey')] = {}
                            
                            all_joints[(ename, 'ex')][fno] = eye["x"]
                            all_joints[(ename, 'ey')][fno] = eye["y"]

                    if "depth" in frame_joints:
                        if ("depth", 'd') not in all_joints:
                            all_joints[("depth", 'd')] = {}

                        all_joints[("depth", 'd')][fno] = frame_joints["depth"]["depth"]

            # スムージング
            for (jname, axis), joints in tqdm(all_joints.items(), desc=f"Filter No.{oidx:03} ... "):
                # filter = OneEuroFilter(freq=30, mincutoff=0.3, beta=0.01, dcutoff=0.25)
                # filter = OneEuroFilter(freq=30, mincutoff=1, beta=0.1, dcutoff=1)
                filter = OneEuroFilter(freq=30, mincutoff=0.5, beta=0.05, dcutoff=0.5)
                for fno, joint in joints.items():
                    all_joints[(jname, axis)][fno] = filter(joint, fno)

            # 出力先ソート済みフォルダ
            smoothed_person_dir_path = os.path.join(args.img_dir, "smooth", f"{oidx:03}")

            os.makedirs(smoothed_person_dir_path, exist_ok=True)

            # 出力
            for frame_json_path in tqdm(frame_json_pathes, desc=f"Save No.{oidx:03} ... "):
                m = frame_pattern.match(os.path.basename(frame_json_path))
                if m:
                    # キーフレの場所を確定（間が空く場合もある）
                    fno = int(m.groups()[0])

                    frame_joints = {}
                    with open(frame_json_path, 'r', encoding='utf-8') as f:
                        frame_joints = json.load(f)
                    
                    # ジョイントグローバル座標を保存
                    for jname, joint in frame_joints["joints"].items():
                        frame_joints["joints"][jname]["x"] = all_joints[(jname, 'x')][fno]
                        frame_joints["joints"][jname]["y"] = all_joints[(jname, 'y')][fno]
                        frame_joints["joints"][jname]["z"] = all_joints[(jname, 'z')][fno]

                    # 表情グローバル座標を保存
                    if "faces" in frame_joints:
                        for fname, face in frame_joints["faces"].items():
                            frame_joints["faces"][fname]["x"] = all_joints[(fname, 'fx')][fno]
                            frame_joints["faces"][fname]["y"] = all_joints[(fname, 'fy')][fno]

                    # 視線グローバル座標を保存
                    if "eyes" in frame_joints:
                        for ename, eye in frame_joints["faces"].items():
                            frame_joints["faces"][ename]["x"] = all_joints[(ename, 'fx')][fno]
                            frame_joints["faces"][ename]["y"] = all_joints[(ename, 'fy')][fno]

                    if "depth" in frame_joints:
                        frame_joints["depth"]["depth"] = all_joints[("depth", 'd')][fno]

                    smooth_json_path = os.path.join(smoothed_person_dir_path, f"smooth_{fno:012}.json")
                    
                    with open(smooth_json_path, 'w', encoding='utf-8') as f:
                        json.dump(frame_joints, f, indent=4)

        logger.info('関節スムージング処理終了: {0}', args.img_dir, decoration=MLogger.DECORATION_BOX)

        return True
    except Exception as e:
        logger.critical("関節スムージングで予期せぬエラーが発生しました。", e, decoration=MLogger.DECORATION_BOX)
        return False
