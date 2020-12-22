# -*- coding: utf-8 -*-
import os
import argparse
import glob
import re
import json
import csv
import datetime
import numpy as np
from numpy.core.defchararray import center
from numpy.lib.function_base import flip
from tqdm import tqdm
import math
from mmd.utils import MServiceUtils

from mmd.utils.MLogger import MLogger
from mmd.utils.MServiceUtils import sort_by_numeric
from lighttrack.visualizer.detection_visualizer import draw_bbox

from mmd.mmd.VmdWriter import VmdWriter
from mmd.module.MMath import MQuaternion, MVector3D, MVector2D, MMatrix4x4, MRect
from mmd.mmd.VmdData import VmdBoneFrame, VmdMorphFrame, VmdMotion, VmdShowIkFrame, VmdInfoIk, OneEuroFilter
from mmd.mmd.PmxData import PmxModel, Bone, Vertex, Bdef1, Ik, IkLink
from mmd.utils.MServiceUtils import get_file_encoding, calc_global_pos, separate_local_qq

logger = MLogger(__name__)

def execute(args):
    try:
        logger.info('モーション生成処理開始: {0}', args.img_dir, decoration=MLogger.DECORATION_BOX)

        if not os.path.exists(args.img_dir):
            logger.error("指定された処理用ディレクトリが存在しません。: {0}", args.img_dir, decoration=MLogger.DECORATION_BOX)
            return False

        if not os.path.exists(os.path.join(args.img_dir, "smooth")):
            logger.error("指定された順番ディレクトリが存在しません。\n順番指定が完了していない可能性があります。: {0}", \
                         os.path.join(args.img_dir, "smooth"), decoration=MLogger.DECORATION_BOX)
            return False

        motion_dir_path = os.path.join(args.img_dir, "motion")
        os.makedirs(motion_dir_path, exist_ok=True)
        
        # モデルをCSVから読み込む
        model = read_bone_csv(args.bone_config)

        # 全人物分の順番別フォルダ
        ordered_person_dir_pathes = sorted(glob.glob(os.path.join(args.img_dir, "smooth", "*")), key=sort_by_numeric)

        smooth_pattern = re.compile(r'^smooth_(\d+)\.')
        start_z = 9999999999

        for oidx, ordered_person_dir_path in enumerate(ordered_person_dir_pathes):    
            logger.info("【No.{0}】FKボーン角度計算開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)

            smooth_json_pathes = sorted(glob.glob(os.path.join(ordered_person_dir_path, "smooth_*.json")), key=sort_by_numeric)

            motion = VmdMotion()
            all_frame_joints = {}
            prev_fno = 9999999999

            right_leg_lengths = []
            left_leg_lengths = []
            leg_lengths = []
            # foot_ys = []
            leg_degrees = []
            flip_fnos = []
        
            for sidx, smooth_json_path in enumerate(tqdm(smooth_json_pathes, desc=f"No.{oidx:03} ... ")):
                m = smooth_pattern.match(os.path.basename(smooth_json_path))
                if m:
                    # キーフレの場所を確定（間が空く場合もある）
                    fno = int(m.groups()[0])

                    frame_joints = {}
                    with open(smooth_json_path, 'r', encoding='utf-8') as f:
                        frame_joints = json.load(f)
                    all_frame_joints[fno] = frame_joints

                    left_hip_vec = get_vec3(all_frame_joints[fno]["joints"], "left_hip")
                    left_foot_vec = get_vec3(all_frame_joints[fno]["joints"], "left_foot")
                    right_hip_vec = get_vec3(all_frame_joints[fno]["joints"], "right_hip")
                    right_foot_vec = get_vec3(all_frame_joints[fno]["joints"], "right_foot")

                    right_leg_lengths.append(right_hip_vec.distanceToPoint(right_foot_vec))
                    left_leg_lengths.append(left_hip_vec.distanceToPoint(left_foot_vec))
                    # 両足の長さを平均
                    leg_lengths.append(np.mean([left_hip_vec.distanceToPoint(left_foot_vec), right_hip_vec.distanceToPoint(right_foot_vec)]))
                    # # 踵の位置を平均
                    # foot_ys.append(np.mean([left_foot_vec.y(), right_foot_vec.y()]))

                    if args.body_motion == 1 or args.face_motion == 1:
                        
                        is_flip = False
                        if prev_fno < fno and sorted(all_frame_joints.keys())[-1] <= sorted(all_frame_joints.keys())[-2] + 2:
                            # 前のキーフレがある場合、体幹に近いボーンが反転していたらスルー(直近が3フレーム以上離れていたらスルーなし)
                            for ljname in ['left_hip', 'left_shoulder']:
                                rjname = ljname.replace('left', 'right')
                                prev_lsign = np.sign(all_frame_joints[prev_fno]["joints"][ljname]["x"] - all_frame_joints[prev_fno]["joints"]["pelvis"]["x"])
                                prev_rsign = np.sign(all_frame_joints[prev_fno]["joints"][rjname]["x"] - all_frame_joints[prev_fno]["joints"]["pelvis"]["x"])
                                lsign = np.sign(all_frame_joints[fno]["joints"][ljname]["x"] - all_frame_joints[fno]["joints"]["pelvis"]["x"])
                                rsign = np.sign(all_frame_joints[fno]["joints"][rjname]["x"] - all_frame_joints[fno]["joints"]["pelvis"]["x"])
                                ldiff = abs(np.diff([all_frame_joints[prev_fno]["joints"][ljname]["x"], all_frame_joints[fno]["joints"][ljname]["x"]]))
                                rdiff = abs(np.diff([all_frame_joints[prev_fno]["joints"][rjname]["x"], all_frame_joints[fno]["joints"][rjname]["x"]]))
                                lrdiff = abs(np.diff([all_frame_joints[fno]["joints"][ljname]["x"], all_frame_joints[fno]["joints"][rjname]["x"]]))

                                if prev_lsign != lsign and prev_rsign != rsign and ldiff > 0.15 and rdiff > 0.15 and lrdiff > 0.15:
                                    is_flip = True
                                    break
                        
                        if is_flip:
                            flip_fnos.append(fno)
                            # 最もデカいのを挿入
                            leg_degrees.append(999999999)
                            continue

                        for jname, (bone_name, name_list, parent_list, ranges, is_hand, is_head) in VMD_CONNECTIONS.items():
                            if name_list is None:
                                continue

                            if not args.hand_motion == 1 and is_hand:
                                # 手トレースは手ON時のみ
                                continue
                            
                            if args.body_motion == 0 and args.face_motion == 1 and not is_head:
                                continue
                            
                            # 前のキーフレから大幅に離れていたらスルー
                            if prev_fno < fno and jname in all_frame_joints[prev_fno]["joints"]:
                                if abs(all_frame_joints[prev_fno]["joints"][jname]["x"] - all_frame_joints[fno]["joints"][jname]["x"]) > 0.1:
                                    continue

                            bf = VmdBoneFrame(fno)
                            bf.set_name(bone_name)
                            
                            if len(name_list) == 4:
                                rotation = calc_direction_qq(bf.fno, motion, frame_joints, *name_list)
                                initial = calc_bone_direction_qq(bf, motion, model, jname, *name_list)
                            else:
                                rotation = calc_direction_qq2(bf.fno, motion, frame_joints, *name_list)
                                initial = calc_bone_direction_qq2(bf, motion, model, jname, *name_list)

                            qq = MQuaternion()
                            for parent_name in reversed(parent_list):
                                qq *= motion.calc_bf(parent_name, bf.fno).rotation.inverted()
                            qq = qq * rotation * initial.inverted()
                            bf.rotation = qq

                            if sidx > 0:
                                prev_fno, _ = motion.get_bone_prev_next_fno(bf.name, fno=bf.fno, is_key=True)
                                prev_bf = motion.calc_bf(bf.name, prev_fno)
                                fno_diff = bf.fno - prev_fno

                                if abs(MQuaternion.dotProduct(prev_bf.rotation, bf.rotation)) >= (1 - (fno_diff * (0.2 if bf.name in ["上半身", "下半身"] else 0.04))):
                                    # 前のキーと大差が無い場合、採用
                                    bf.key = True
                                else:
                                    # フリップしている可能性がある場合、キー登録なし
                                    bf.key = False
                                    if bf.name in ["上半身", "下半身"]:
                                        flip_fnos.append(fno)
                            else:
                                bf.key = True

                            motion.regist_bf(bf, bf.name, bf.fno, is_key=bf.key)
                                
                    if "faces" in frame_joints and args.face_motion == 1:
                        # 表情がある場合出力
                        # まばたき・視線の向き
                        left_eye_euler = calc_left_eye(fno, motion, frame_joints)
                        right_eye_euler = calc_right_eye(fno, motion, frame_joints)
                        blend_eye(fno, motion, left_eye_euler, right_eye_euler)

                        # 口
                        calc_lip(fno, motion, frame_joints)

                        # 眉
                        calc_eyebrow(fno, motion, frame_joints)
                    
                    prev_fno = fno

            if args.body_motion == 1:
                logger.info("【No.{0}】FKボーン角度チェック開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)

                for fidx, (fno, frame_joints) in enumerate(tqdm(all_frame_joints.items(), desc=f"No.{oidx:03} ... ")):
                    for jname, (bone_name, name_list, parent_list, ranges, is_hand, is_head) in VMD_CONNECTIONS.items():
                        if name_list is None:
                            continue
                        
                        if not args.hand_motion == 1 and is_hand:
                            # 手トレースは手ON時のみ
                            continue
                        
                        if args.body_motion == 0 and args.face_motion == 1 and not is_head:
                            continue
                        
                        bf = motion.calc_bf(bone_name, fno)

                        if ranges:
                            # 可動域指定がある場合、制限する
                            local_x_axis = model.get_local_x_axis(bf.name)
                            x_qq, y_qq, z_qq, _ = separate_local_qq(bf.fno, bf.name, bf.rotation, local_x_axis)
                            local_z_axis = MVector3D(0, 0, (-1 if "right" in jname else 1))
                            local_y_axis = MVector3D.crossProduct(local_x_axis, local_z_axis)
                            x_limited_qq = MQuaternion.fromAxisAndAngle(local_x_axis, max(ranges["x"]["min"], min(ranges["x"]["max"], x_qq.toDegree() * MVector3D.dotProduct(local_x_axis, x_qq.vector()))))
                            y_limited_qq = MQuaternion.fromAxisAndAngle(local_y_axis, max(ranges["y"]["min"], min(ranges["y"]["max"], y_qq.toDegree() * MVector3D.dotProduct(local_y_axis, y_qq.vector()))))
                            z_limited_qq = MQuaternion.fromAxisAndAngle(local_z_axis, max(ranges["z"]["min"], min(ranges["z"]["max"], z_qq.toDegree() * MVector3D.dotProduct(local_z_axis, z_qq.vector()))))
                            bf.rotation = y_limited_qq * x_limited_qq * z_limited_qq

                            motion.regist_bf(bf, bf.name, bf.fno, is_key=bf.key)

                    if fno not in flip_fnos:
                        # センター・グルーブ・足IKは初期値
                        center_bf = VmdBoneFrame(fno)
                        center_bf.set_name("センター")
                        motion.regist_bf(center_bf, center_bf.name, fno)

                        groove_bf = VmdBoneFrame(fno)
                        groove_bf.set_name("グルーブ")
                        motion.regist_bf(groove_bf, groove_bf.name, fno)

                        left_leg_ik_bf = VmdBoneFrame(fno)
                        left_leg_ik_bf.set_name("左足ＩＫ")
                        motion.regist_bf(left_leg_ik_bf, left_leg_ik_bf.name, fno)

                        right_leg_ik_bf = VmdBoneFrame(fno)
                        right_leg_ik_bf.set_name("右足ＩＫ")
                        motion.regist_bf(right_leg_ik_bf, right_leg_ik_bf.name, fno)

                        right_leg_bf = motion.calc_bf("右足", fno)
                        right_knee_bf = motion.calc_bf("右ひざ", fno)
                        left_leg_bf = motion.calc_bf("左足", fno)
                        left_knee_bf = motion.calc_bf("左ひざ", fno)
                        leg_degrees.append(right_leg_bf.rotation.toDegree() + right_knee_bf.rotation.toDegree() + left_leg_bf.rotation.toDegree() + left_knee_bf.rotation.toDegree())
                    else:
                        # 最もデカいのを挿入
                        leg_degrees.append(99999999)


                logger.info("【No.{0}】直立姿勢計算開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)
                
                # fnos = list(all_frame_joints.keys())
                # # 両足が最も直立している（距離が長いフレーム）を対象とする
                # max_leg_fidxs = list(reversed(np.argsort(leg_lengths)))
                # # # 踵がもっとも値が小さい（最も下）を対象とする
                # # ground_fidxs = np.argsort(foot_ys)
                # 足とひざの角度が最も小さい（最も伸びている）を対象とする
                degree_fidxs = np.argsort(leg_degrees)
                upright_fidx = degree_fidxs[0]
                upright_fno = list(all_frame_joints.keys())[upright_fidx]
                # upright_fidx = -1
                # for limit in range(50, fnos[-1], 50):
                #     upright_fidxs_set = set(max_leg_fidxs[:limit]) & set(degree_fidxs[:limit])
                #     upright_fidxs = [x for x in degree_fidxs if x in upright_fidxs_set]
                #     if len(upright_fidxs) > 0:
                #         upright_fidx = upright_fidxs[0]
                #         break
                # upright_fidx = 0 if upright_fidx < 0 else upright_fidx
                # # 直立キーフレ
                # upright_fno = list(all_frame_joints.keys())[upright_fidx]
                # 直立の足の長さ
                upright_right_leg_length = right_leg_lengths[upright_fidx]
                upright_left_leg_length = left_leg_lengths[upright_fidx]

                # 直立キーフレの骨盤は地に足がついているとみなす
                upright_pelvis_vec = get_pelvis_vec(all_frame_joints, upright_fno, args)

                logger.info("【No.{0}】直立キーフレ: {1}", f"{oidx:03}", upright_fno)

                # つま先末端までのリンク
                right_toe_links = model.create_link_2_top_one("右つま先", is_defined=False)
                left_toe_links = model.create_link_2_top_one("左つま先", is_defined=False)

                logger.info("【No.{0}】センター計算開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)

                for fidx, (fno, frame_joints) in enumerate(tqdm(all_frame_joints.items(), desc=f"No.{oidx:03} ... ")):
                    if fno in flip_fnos:
                        # フリップはセンター計算もおかしくなるのでスキップ
                        continue

                    pelvis_vec = get_pelvis_vec(all_frame_joints, fno, args)

                    if start_z == 9999999999:
                        # 最初の人物の深度を0にする
                        start_z = pelvis_vec.z()
                    
                    pelvis_y = pelvis_vec.y() - upright_pelvis_vec.y()

                    # Yはグルーブ
                    groove_bf = VmdBoneFrame()
                    groove_bf.fno = fno
                    groove_bf.set_name("グルーブ")                    
                    groove_bf.position.setY(pelvis_y)
                    motion.regist_bf(groove_bf, groove_bf.name, fno)

                    center_bf = VmdBoneFrame()
                    center_bf.fno = fno
                    center_bf.set_name("センター")

                    # XZはセンター
                    center_bf.position.setX(pelvis_vec.x())
                    center_bf.position.setZ(pelvis_vec.z() - start_z)
                    # center_bf.position.setY(pelvis_vec.y())
                    # center_bf.position.setZ(pelvis_vec.z())
                    motion.regist_bf(center_bf, center_bf.name, fno)

                    # つま先からのグルーブ調整
                    min_leg_ys = []
                    for direction, toe_links, now_leg_length, upright_leg_length in [("右", right_toe_links, right_leg_lengths[fidx], upright_right_leg_length), \
                                                                                     ("左", left_toe_links, left_leg_lengths[fidx], upright_left_leg_length)]:
                        toe_ik_3ds_dic = calc_global_pos(model, toe_links, motion, fno)
                        toe_y = toe_ik_3ds_dic[toe_links.last_name()].y()

                        ankle_bf = motion.calc_bf(f"{direction}足首", fno)

                        # 大体床に接地しているっぽい時はその分をマイナスする
                        # 足がつま先立っていない場合、床に接地と見なす
                        ankle_x_qq, _, _, _ = MServiceUtils.separate_local_qq(fno, ankle_bf.name, ankle_bf.rotation, MVector3D(1, 0, 0))
                        if ankle_x_qq.toDegree() < 30:
                            min_leg_ys.append(toe_y)
                
                    # 大体床に接地していると見なしてマイナス(地面にめり込んでたら上げる)
                    if len(min_leg_ys) > 0:
                        pelvis_y -= min(min_leg_ys)

                    groove_bf = VmdBoneFrame()
                    groove_bf.fno = fno
                    groove_bf.set_name("グルーブ")
                    
                    if args.upper_motion == 1:
                        # 上半身モーションの場合、グルーブを0に強制変換
                        groove_bf.position.setY(0)
                    else:
                        # 調整後のグルーブ登録
                        groove_bf.position.setY(pelvis_y)

                    motion.regist_bf(groove_bf, groove_bf.name, fno)

                    if fidx > 0:
                        prev_fno, _ = motion.get_bone_prev_next_fno(groove_bf.name, fno=groove_bf.fno, is_key=True)
                        prev_bf = motion.calc_bf(groove_bf.name, prev_fno)
                        fno_diff = groove_bf.fno - prev_fno

                        if abs(pelvis_y - prev_bf.position.y()) > 0.25 * fno_diff:
                            # 差がでかい場合、削除
                            del motion.bones[groove_bf.name][fno]

                logger.info("【No.{0}】センタースムージング開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)
                xfilter = OneEuroFilter(freq=30, mincutoff=0.1, beta=0.05, dcutoff=0.1)
                yfilter = OneEuroFilter(freq=30, mincutoff=0.1, beta=0.05, dcutoff=0.1)
                zfilter = OneEuroFilter(freq=30, mincutoff=0.1, beta=0.05, dcutoff=0.1)
                for fidx, (fno, frame_joints) in enumerate(tqdm(all_frame_joints.items(), desc=f"No.{oidx:03} ... ")):
                    center_bf = motion.calc_bf("センター", fno)
                    center_bf.position.setX(xfilter(center_bf.position.x(), fno))
                    center_bf.position.setZ(zfilter(center_bf.position.z(), fno))

                    groove_bf = motion.calc_bf("グルーブ", fno)
                    groove_bf.position.setY(yfilter(groove_bf.position.y(), fno))

                    if fno not in flip_fnos:
                        motion.regist_bf(center_bf, center_bf.name, fno, is_key=center_bf.key)
                        motion.regist_bf(groove_bf, groove_bf.name, fno, is_key=groove_bf.key)

                logger.info("【No.{0}】左足IK計算開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)
                convert_leg_fk2ik(oidx, motion, model, flip_fnos, "左")

                logger.info("【No.{0}】右足IK計算開始", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)
                convert_leg_fk2ik(oidx, motion, model, flip_fnos, "右")

                # # つま先IK末端までのリンク
                # right_toe_ik_links = model.create_link_2_top_one("右つま先ＩＫ", is_defined=False)
                # left_toe_ik_links = model.create_link_2_top_one("左つま先ＩＫ", is_defined=False)

                # # 直立フレームは地に足がついていると見なす
                # logger.info("【No.{0}】センター・足ＩＫ調整", f"{oidx:03}", decoration=MLogger.DECORATION_LINE)

                # prev_center_bf = None
                # prev_right_leg_ik_bf = None
                # prev_left_leg_ik_bf = None
                # for fidx, (fno, frame_joints) in enumerate(tqdm(all_frame_joints.items(), desc=f"No.{oidx:03} ... ")):                    
                #     if fno in flip_fnos:
                #         continue

                #     right_leg_ik_bf = motion.calc_bf("右足ＩＫ", fno)
                #     left_leg_ik_bf = motion.calc_bf("左足ＩＫ", fno)

                #     if fidx > 0:
                #         leg_z_diffs = []
                #         # 1番目以降で、ＩＫのＹが0の場合、ＩＫを動かさない（センターの動きも止める）
                #         if right_leg_ik_bf.position.y() < 1:
                #             leg_z_diffs.append(right_leg_ik_bf.position.z() - prev_right_leg_ik_bf.position.z())
                #         # 1番目以降で、ＩＫのＹが0の場合、ＩＫを動かさない（センターの動きも止める）
                #         if left_leg_ik_bf.position.y() < 1:
                #             leg_z_diffs.append(left_leg_ik_bf.position.z() - prev_left_leg_ik_bf.position.z())

                #         z_idx = np.abs(np.asarray(leg_z_diffs) - 0).argmin() if len(leg_z_diffs) > 0 else -1
                #         if z_idx >= 0:
                #             # zが調整対象である場合
                #             center_bf = motion.calc_bf("センター", fno)

                #             center_bf.position.setZ(center_bf.position.z() - leg_z_diffs[z_idx])
                #             right_leg_ik_bf.position.setZ(right_leg_ik_bf.position.z() - leg_z_diffs[z_idx])
                #             left_leg_ik_bf.position.setZ(left_leg_ik_bf.position.z() - leg_z_diffs[z_idx])

                #             motion.regist_bf(center_bf, center_bf.name, fno)
                #             motion.regist_bf(right_leg_ik_bf, right_leg_ik_bf.name, fno)
                #             motion.regist_bf(left_leg_ik_bf, left_leg_ik_bf.name, fno)

                #     prev_right_leg_ik_bf = right_leg_ik_bf
                #     prev_left_leg_ik_bf = left_leg_ik_bf

                #     if args.upper_motion == 1:
                #         # 上半身モーションの場合、グルーブを0に強制変換
                #         groove_bf = motion.calc_bf("グルーブ", fno)
                #         groove_bf.setY(0)
                #         motion.regist_bf(groove_bf, groove_bf.name, fno)

                #     # つま先のグローバル位置がマイナスの場合、その分を上げる
                #     for toe_ik_links, leg_ik_bf in [(right_toe_ik_links, right_leg_ik_bf), (left_toe_ik_links, left_leg_ik_bf)]:
                #         toe_ik_3ds_dic = calc_global_pos(model, toe_ik_links, motion, fno)
                #         toe_y = toe_ik_3ds_dic[toe_ik_links.last_name()].y()
                #         if toe_y < 0:
                #             leg_ik_bf.position.setY(leg_ik_bf.position.y() + -toe_y)
                #             motion.regist_bf(leg_ik_bf, leg_ik_bf.name, fno)

            # # 手首を削除
            # if "右手首" in motion.bones:
            #     del motion.bones["右手首"]
            # if "左手首" in motion.bones:
            #     del motion.bones["左手首"]
            
            motion_path = os.path.join(motion_dir_path, "output_{0}_no{1:03}.vmd".format(datetime.datetime.now().strftime('%Y%m%d_%H%M%S'), oidx))
            writer = VmdWriter(model, motion, motion_path)
            writer.write()

            logger.info("【No.{0}】モーション生成終了: {1}", f"{oidx:03}", motion_path, decoration=MLogger.DECORATION_BOX)

        logger.info('モーション生成処理全件終了', decoration=MLogger.DECORATION_BOX)

        return True
    except Exception as e:
        logger.critical("モーション生成で予期せぬエラーが発生しました。", e, decoration=MLogger.DECORATION_BOX)
        return False

def get_pelvis_vec(all_frame_joints: dict, fno: int, args):
    # 画像サイズ
    image_size = np.array([all_frame_joints[fno]["image"]["width"], all_frame_joints[fno]["image"]["height"]])

    #bbox
    bbox_pos = np.array([all_frame_joints[fno]["bbox"]["x"], all_frame_joints[fno]["bbox"]["y"]])
    bbox_size = np.array([all_frame_joints[fno]["bbox"]["width"], all_frame_joints[fno]["bbox"]["height"]])

    # 骨盤のカメラの中心からの相対位置
    pelvis_vec = (get_vec3(all_frame_joints[fno]["joints"], "pelvis") + get_vec3(all_frame_joints[fno]["joints"], "spine1")) / 2
    # pelvis_pos = np.array([(center_vec.x() / 2) + 0.5, (center_vec.y() / -2) + 0.5])
    # bbox左上を原点とした相対位置
    pelvis_pos = np.array([(pelvis_vec.x() / 2) + 0.5, (pelvis_vec.y() / -2) + 0.5])

    # 骨盤の画面内グローバル位置
    pelvis_3d_global_pos = (bbox_size * pelvis_pos) + bbox_pos
    pelvis_global_pos = (np.array([all_frame_joints[fno]["proj_joints"]["pelvis"]["x"], all_frame_joints[fno]["proj_joints"]["pelvis"]["y"]]) + \
                         np.array([all_frame_joints[fno]["proj_joints"]["spine1"]["x"], all_frame_joints[fno]["proj_joints"]["spine1"]["y"]])) / 2
    pelvis_global_pos[0] -= image_size[0] / 2
    # pelvis_global_pos[1] = image_size[1] / 2 - pelvis_global_pos[1]
    # pelvis_global_pos[1] -= image_size[1] * 0.6

    # カメラ中央
    camera_center_pos = np.array([all_frame_joints[fno]["others"]["center"]["x"], all_frame_joints[fno]["others"]["center"]["y"]])
    # カメラ倍率
    camera_scale = all_frame_joints[fno]["camera"]["scale"]
    # センサー幅（画角？）
    sensor_width = all_frame_joints[fno]["others"]["sensor_width"]
    # camera_center_pos -= image_size / 2
    # Zはカメラ深度
    depth = all_frame_joints[fno]["depth"]["depth"]

    # # 骨盤の画面全体からの相対位置
    # pelvis_relative_pos = pelvis_global_pos / image_size
    # # X相対位置は画面中央に基づく
    # pelvis_relative_pos[0] -= 0.5
    # # 画面中央をゼロとする
    # pelvis_global_pos *= image_size

    # 1ミクセルは8cm(80mm)
    focal_length_in_px = all_frame_joints[fno]["others"]["focal_length_in_px"]

    # モデル座標系
    model_view = create_model_view(camera_center_pos, camera_scale, image_size, depth, focal_length_in_px)
    # プロジェクション座標系
    projection_view = create_projection_view(image_size, sensor_width)

    # viewport
    viewport_rect = MRect(0, 0, args.center_scale * 10, args.center_scale * 10)
    # viewport_rect = MRect(-(image_size[0] / 2), 0, image_size[0], image_size[1])
    # viewport_rect = MRect(-(image_size[0] / 2), -image_size[1], image_size[0], image_size[1])
    # viewport_rect = MRect(-(16 / 2 / 10), 0, 16 / 10, 9 / 10)
    # viewport_rect = MRect(-(10 / 2), 0, 10, 10)

    # pelvis_project_vec = MVector3D(pelvis_global_pos[0] - (image_size[0] / 2), image_size[1] - pelvis_global_pos[1] - (image_size[1] / 2), 0)
    pelvis_project_vec = MVector3D(pelvis_global_pos[0], pelvis_global_pos[1], 1)

    # 逆プロジェクション座標位置
    pelvis_global_vec = pelvis_project_vec.unproject(model_view, projection_view, viewport_rect)

    # 骨盤のグローバル相対位置
    # pelvis_relative_vec = MVector3D((pelvis_global_vec.x() - (image_size[0] / 2)) * 20, (image_size[1] - pelvis_global_vec.y() - (image_size[1] / 2)) * 20, pelvis_z / camera_scale / 2)

    # floor_position_vec = MVector3D(10, 10, 10)
    # floor_direction_vec = MVector3D(0, -1, 0)

    # fe = floor_position_vec - eye_position_vec
    # pe = pelvis_global_vec - eye_position_vec
    # pe *= 30
    # pe *= MVector3D.dotProduct(floor_direction_vec.normalized(), fe.normalized()) / MVector3D.dotProduct(floor_direction_vec.normalized(), pe.normalized())
    pelvis_mmd_vec = MVector3D(pelvis_global_vec.x(), pelvis_global_vec.y(), depth * args.center_scale)

    return pelvis_mmd_vec

    # # 骨盤の画面全体からの相対位置
    # pelvis_relative_pos = pelvis_global_pos / image_size
    # # # X相対位置は画面中央に基づく
    # # pelvis_relative_pos[0] -= 0.5

    # # # Y相対位置は直立からの位置に基づく
    # # if upright_y != 0:
    # #     pelvis_relative_pos[1] -= (upright_y / args.center_scale / 10)

    # pelvis_vec = MVector3D()
    # pelvis_vec.setX(pelvis_relative_pos[0] * args.center_scale)
    # pelvis_vec.setY(pelvis_relative_pos[1] * args.center_scale / 10)
    # pelvis_vec.setZ(depth * args.center_scale / 25)

    # return pelvis_vec


def get_eye_position(model_view: MMatrix4x4):
    mat = model_view.inverted()
    return MVector3D(mat.data()[0, 3], mat.data()[1, 3], mat.data()[2, 3])


# モデル座標系作成
def create_model_view(camera_center_pos: list, camera_scale: float, image_size: list, depth: float, focal_length_in_px: float):
    # モデル座標系（原点を見るため、単位行列）
    model_view = MMatrix4x4()
    model_view.setToIdentity()   

    camera_eye = MVector3D(image_size[0] / 2, image_size[1] / 2, focal_length_in_px)
    # camera_pos = MVector3D(0, 13.30119, -0.255278)
    # camera_pos = MVector3D(0, 0, 0)
    # camera_pos = MVector3D(camera_center_pos[0], camera_center_pos[1], length)
    camera_pos = MVector3D(camera_center_pos[0], camera_center_pos[1], depth)
    # camera_qq = MQuaternion.rotationTo(camera_eye.normalized(), camera_pos.normalized())

    # # カメラの原点（グローバル座標）
    # mat_origin = MMatrix4x4()
    # mat_origin.setToIdentity()
    # mat_origin.translate(camera_pos)
    # # mat_origin.rotate(camera_qq)
    # # mat_origin.translate(MVector3D(0, 0, -length))
    # mat_origin.scale(camera_scale)
    # camera_center = mat_origin * MVector3D()

    # mat_up = MMatrix4x4()
    # mat_up.setToIdentity()
    # # mat_up.rotate(camera_qq)
    # camera_up = mat_up * MVector3D(0, 1, 0)

    # カメラ座標系の行列
    # eye: カメラの原点（グローバル座標）
    # center: カメラの注視点（グローバル座標）
    # up: カメラの上方向ベクトル
    model_view.lookAt(camera_eye, camera_pos, MVector3D(0, 1, 0))

    return model_view

# プロジェクション座標系作成
def create_projection_view(image_size: list, sensor_width: float):
    mat = MMatrix4x4()
    mat.setToIdentity()
    mat.perspective(sensor_width, image_size[0] / image_size[1], 0, 50000)

    return mat

# 足ＩＫ変換処理実行
def convert_leg_fk2ik(oidx: int, motion: VmdMotion, model: PmxModel, flip_fnos: list, direction: str):
    leg_ik_bone_name = "{0}足ＩＫ".format(direction)
    toe_ik_bone_name = "{0}つま先ＩＫ".format(direction)
    leg_bone_name = "{0}足".format(direction)
    knee_bone_name = "{0}ひざ".format(direction)
    ankle_bone_name = "{0}足首".format(direction)

    # 足FK末端までのリンク
    fk_links = model.create_link_2_top_one(ankle_bone_name, is_defined=False)
    # 足IK末端までのリンク
    ik_links = model.create_link_2_top_one(leg_ik_bone_name, is_defined=False)
    # つま先IK末端までのリンク
    toe_ik_links = model.create_link_2_top_one(toe_ik_bone_name, is_defined=False)
    # つま先（足首の子ボーン）の名前
    ankle_child_bone_name = model.bone_indexes[model.bones[toe_ik_bone_name].ik.target_index]
    # つま先末端までのリンク
    toe_fk_links = model.create_link_2_top_one(ankle_child_bone_name, is_defined=False)

    fnos = motion.get_bone_fnos(leg_bone_name, knee_bone_name, ankle_bone_name)

    ik_parent_name = ik_links.get(leg_ik_bone_name, offset=-1).name
    default_vec = MVector3D(1, 0, 0) if "左" == direction else MVector3D(-1, 0, 0)

    # # まずキー登録
    # logger.info("【No.{0}】{1}足IK準備", f"{oidx:03}", direction)
    # fno = 0
    # for fno in tqdm(fnos, desc=f"No.{oidx:03} ... "):
    #     bf = motion.calc_bf(leg_ik_bone_name, fno)
    #     motion.regist_bf(bf, leg_ik_bone_name, fno)

    # 足IKの移植
    # logger.info("【No.{0}】{1}足IK移植", f"{oidx:03}", direction)
    fno = 0
    for fno in tqdm(fnos, desc=f"No.{oidx:03} ... "):
        if fno in flip_fnos:
            # フリップはセンター計算もおかしくなるのでスキップ
            continue

        leg_fk_3ds_dic = calc_global_pos(model, fk_links, motion, fno)
        _, leg_ik_matrixs = calc_global_pos(model, ik_links, motion, fno, return_matrix=True)

        # 足首の角度がある状態での、つま先までのグローバル位置
        leg_toe_fk_3ds_dic = calc_global_pos(model, toe_fk_links, motion, fno)

        # IKの親から見た相対位置
        leg_ik_parent_matrix = leg_ik_matrixs[ik_parent_name]

        bf = motion.calc_bf(leg_ik_bone_name, fno)
        # 足ＩＫの位置は、足ＩＫの親から見た足首のローカル位置（足首位置マイナス）
        bf.position = leg_ik_parent_matrix.inverted() * (leg_fk_3ds_dic[ankle_bone_name] - (model.bones[ankle_bone_name].position - model.bones[ik_parent_name].position))
        bf.rotation = MQuaternion()

        # 一旦足ＩＫの位置が決まった時点で登録
        motion.regist_bf(bf, leg_ik_bone_name, fno)
        # 足ＩＫ回転なし状態でのつま先までのグローバル位置
        leg_ik_3ds_dic, leg_ik_matrisxs = calc_global_pos(model, toe_ik_links, motion, fno, return_matrix=True)

        # つま先のローカル位置
        ankle_child_initial_local_pos = leg_ik_matrisxs[leg_ik_bone_name].inverted() * leg_ik_3ds_dic[toe_ik_bone_name]
        ankle_child_local_pos = leg_ik_matrisxs[leg_ik_bone_name].inverted() * leg_toe_fk_3ds_dic[ankle_child_bone_name]

        # 足ＩＫの回転は、足首から見たつま先の方向
        bf.rotation = MQuaternion.rotationTo(ankle_child_initial_local_pos, ankle_child_local_pos)

        # 床に近く、足がつま先立っていない場合、床に接地と見なして調整
        ik_x_qq, _, _, ik_yz_qq = MServiceUtils.separate_local_qq(fno, bf.name, bf.rotation, default_vec)
        if 1 > bf.position.y() and 20 > ik_x_qq.toDegree():
            groove_bf = motion.calc_bf("グルーブ", fno)
            groove_bf.position.setY(groove_bf.position.y() - bf.position.y())
            bf.position.setY(0)
            bf.rotation = ik_yz_qq

        motion.regist_bf(bf, leg_ik_bone_name, fno)
        
        # つま先のグローバル位置がマイナスの場合、その分を上げる
        toe_ik_3ds_dic = calc_global_pos(model, toe_ik_links, motion, fno)
        toe_y = toe_ik_3ds_dic[toe_ik_links.last_name()].y()
        if toe_y < 0:
            bf.position.setY(bf.position.y() + -toe_y)
            motion.regist_bf(bf, bf.name, fno)


def calc_direction_qq(bf: VmdBoneFrame, motion: VmdMotion, joints: dict, direction_from_name: str, direction_to_name: str, up_from_name: str, up_to_name: str):
    direction_from_vec = get_vec3(joints["joints"], direction_from_name)
    direction_to_vec = get_vec3(joints["joints"], direction_to_name)
    up_from_vec = get_vec3(joints["joints"], up_from_name)
    up_to_vec = get_vec3(joints["joints"], up_to_name)

    direction = (direction_to_vec - direction_from_vec).normalized()
    up = (up_to_vec - up_from_vec).normalized()
    cross = MVector3D.crossProduct(direction, up)
    qq = MQuaternion.fromDirection(direction, cross)

    return qq

def calc_direction_qq2(bf: VmdBoneFrame, motion: VmdMotion, joints: dict, direction_from_name: str, direction_to_name: str, up_from_name: str, up_to_name: str, cross_from_name: str, cross_to_name: str):
    direction_from_vec = get_vec3(joints["joints"], direction_from_name)
    direction_to_vec = get_vec3(joints["joints"], direction_to_name)
    up_from_vec = get_vec3(joints["joints"], up_from_name)
    up_to_vec = get_vec3(joints["joints"], up_to_name)
    cross_from_vec = get_vec3(joints["joints"], cross_from_name)
    cross_to_vec = get_vec3(joints["joints"], cross_to_name)

    direction = (direction_to_vec - direction_from_vec).normalized()
    up = (up_to_vec - up_from_vec).normalized()
    cross = (cross_to_vec - cross_from_vec).normalized()
    qq = MQuaternion.fromDirection(direction, MVector3D.crossProduct(up, cross))

    return qq

def calc_finger(bf: VmdBoneFrame, motion: VmdMotion, model: PmxModel, jname: str, default_joints: dict, frame_joints: dict, name_list: list, parent_list: list):
    rotation = calc_direction_qq(bf.fno, motion, frame_joints, *name_list)
    bone_initial = calc_bone_direction_qq(bf, motion, model, jname, *name_list)

    qq = MQuaternion()
    for parent_name in reversed(parent_list):
        qq *= motion.calc_bf(parent_name, bf.fno).rotation.inverted()
    qq = qq * rotation * bone_initial.inverted()

    _, _, z_qq, _ = separate_local_qq(bf.fno, bf.name, qq, model.get_local_x_axis(bf.name))
    z_limited_qq = MQuaternion.fromAxisAndAngle(MVector3D(0, 0, -1 * (-1 if "right" in jname else 1)), min(90, z_qq.toDegree()))
    bf.rotation = z_limited_qq

    motion.regist_bf(bf, bf.name, bf.fno)

def calc_bone_direction_qq(bf: VmdBoneFrame, motion: VmdMotion, model: PmxModel, jname: str, direction_from_name: str, direction_to_name: str, up_from_name: str, up_to_name: str):
    direction_from_vec = get_bone_vec3(model, direction_from_name)
    direction_to_vec = get_bone_vec3(model, direction_to_name)
    up_from_vec = get_bone_vec3(model, up_from_name)
    up_to_vec = get_bone_vec3(model, up_to_name)

    direction = (direction_to_vec - direction_from_vec).normalized()
    up = (up_to_vec - up_from_vec).normalized()
    qq = MQuaternion.fromDirection(direction, up)

    return qq

def calc_bone_direction_qq2(bf: VmdBoneFrame, motion: VmdMotion, model: PmxModel, jname: str, direction_from_name: str, direction_to_name: str, up_from_name: str, up_to_name: str, cross_from_name: str, cross_to_name: str):
    direction_from_vec = get_bone_vec3(model, direction_from_name)
    direction_to_vec = get_bone_vec3(model, direction_to_name)
    up_from_vec = get_bone_vec3(model, up_from_name)
    up_to_vec = get_bone_vec3(model, up_to_name)
    cross_from_vec = get_bone_vec3(model, cross_from_name)
    cross_to_vec = get_bone_vec3(model, cross_to_name)

    direction = (direction_to_vec - direction_from_vec).normalized()
    up = (up_to_vec - up_from_vec).normalized()
    cross = (cross_to_vec - cross_from_vec).normalized()
    qq = MQuaternion.fromDirection(direction, MVector3D.crossProduct(up, cross))

    return qq

def get_bone_vec3(model: PmxModel, joint_name: str):
    bone_name, _, _, _, _, _ = VMD_CONNECTIONS[joint_name]
    if bone_name in model.bones:
        return model.bones[bone_name].position
    
    return MVector3D()

def get_vec3(joints: dict, jname: str):
    if jname in joints:
        joint = joints[jname]
        return MVector3D(joint["x"], joint["y"], joint["z"])
    else:
        if jname == "pelvis2":
            # 尾てい骨くらい
            right_hip_vec = get_vec3(joints, "right_hip")
            left_hip_vec = get_vec3(joints, "left_hip")
            return (right_hip_vec + left_hip_vec) / 2
    
    return MVector3D()


def read_bone_csv(bone_csv_path: str):
    model = PmxModel()
    model.name = os.path.splitext(os.path.basename(bone_csv_path))[0]

    with open(bone_csv_path, "r", encoding=get_file_encoding(bone_csv_path)) as f:
        reader = csv.reader(f)

        # 列名行の代わりにルートボーン登録
        # サイジング用ルートボーン
        sizing_root_bone = Bone("SIZING_ROOT_BONE", "SIZING_ROOT_BONE", MVector3D(), -1, 0, 0)
        sizing_root_bone.index = -999

        model.bones[sizing_root_bone.name] = sizing_root_bone
        # インデックス逆引きも登録
        model.bone_indexes[sizing_root_bone.index] = sizing_root_bone.name

        for ridx, row in enumerate(reader):
            if row[0] == "Bone":
                bone = Bone(row[1], row[2], MVector3D(float(row[5]), float(row[6]), float(row[7])), row[13], int(row[3]), \
                            int(row[8]) | int(row[9]) | int(row[10]) | int(row[11]) | int(row[12]))
                bone.index = ridx - 1

                if len(row[37]) > 0:
                    # IKターゲットがある場合、IK登録
                    bone.ik = Ik(model.bones[row[37]].index, int(row[38]), math.radians(float(row[39])))

                model.bones[row[1]] = bone
                model.bone_indexes[bone.index] = row[1]
            elif row[0] == "IKLink":
                iklink = IkLink(model.bones[row[2]].index, int(row[3]), MVector3D(float(row[4]), float(row[6]), float(row[8])), MVector3D(float(row[5]), float(row[7]), float(row[9])))
                model.bones[row[1]].ik.link.append(iklink)
    
    for bidx, bone in model.bones.items():
        # 親ボーンINDEXの設定
        if bone.parent_index and bone.parent_index in model.bones:
            bone.parent_index = model.bones[bone.parent_index].index
        else:
            bone.parent_index = -1

    # 首根元ボーン
    if "左肩" in model.bones and "右肩" in model.bones:
        neck_base_vertex = Vertex(-1, (model.bones["左肩"].position + model.bones["右肩"].position) / 2 + MVector3D(0, -0.1, 0), MVector3D(), [], [], Bdef1(-1), -1)
        neck_base_vertex.position.setX(0)
        neck_base_bone = Bone("首根元", "base of neck", neck_base_vertex.position.copy(), -1, 0, 0)

        if "上半身2" in model.bones:
            # 上半身2がある場合、表示先は、上半身2
            neck_base_bone.parent_index = model.bones["上半身2"].index
            neck_base_bone.tail_index = model.bones["上半身2"].index
        elif "上半身" in model.bones:
            neck_base_bone.parent_index = model.bones["上半身"].index
            neck_base_bone.tail_index = model.bones["上半身"].index

        neck_base_bone.index = len(model.bones.keys())
        model.bones[neck_base_bone.name] = neck_base_bone
        model.bone_indexes[neck_base_bone.index] = neck_base_bone.name

    # 鼻ボーン
    if "頭" in model.bones and "首" in model.bones:
        nose_bone = Bone("鼻", "nose", MVector3D(0, model.bones["頭"].position.y(), model.bones["頭"].position.z() - 0.5), -1, 0, 0)
        nose_bone.parent_index = model.bones["首"].index
        nose_bone.tail_index = model.bones["頭"].index
        nose_bone.index = len(model.bones.keys())
        model.bones[nose_bone.name] = nose_bone
        model.bone_indexes[nose_bone.index] = nose_bone.name

    # 頭頂ボーン
    if "頭" in model.bones:
        head_top_bone = Bone("頭頂", "top of head", MVector3D(0, model.bones["頭"].position.y() + 1, 0), -1, 0, 0)
        head_top_bone.parent_index = model.bones["鼻"].index
        head_top_bone.index = len(model.bones.keys())
        model.bones[head_top_bone.name] = head_top_bone
        model.bone_indexes[head_top_bone.index] = head_top_bone.name

    if "右つま先" in model.bones:
        right_big_toe_bone = Bone("右足親指", "", model.bones["右つま先"].position + MVector3D(0.5, 0, 0), -1, 0, 0)
        right_big_toe_bone.parent_index = model.bones["右足首"].index
        right_big_toe_bone.index = len(model.bones.keys())
        model.bones[right_big_toe_bone.name] = right_big_toe_bone
        model.bone_indexes[right_big_toe_bone.index] = right_big_toe_bone.name

        right_small_toe_bone = Bone("右足小指", "", model.bones["右つま先"].position + MVector3D(-0.5, 0, 0), -1, 0, 0)
        right_small_toe_bone.parent_index = model.bones["右足首"].index
        right_small_toe_bone.index = len(model.bones.keys())
        model.bones[right_small_toe_bone.name] = right_small_toe_bone
        model.bone_indexes[right_small_toe_bone.index] = right_small_toe_bone.name

    if "左つま先" in model.bones:
        left_big_toe_bone = Bone("左足親指", "", model.bones["左つま先"].position + MVector3D(-0.5, 0, 0), -1, 0, 0)
        left_big_toe_bone.parent_index = model.bones["左足首"].index
        left_big_toe_bone.index = len(model.bones.keys())
        model.bones[left_big_toe_bone.name] = left_big_toe_bone
        model.bone_indexes[left_big_toe_bone.index] = left_big_toe_bone.name

        left_small_toe_bone = Bone("左足小指", "", model.bones["左つま先"].position + MVector3D(0.5, 0, 0), -1, 0, 0)
        left_small_toe_bone.parent_index = model.bones["左足首"].index
        left_small_toe_bone.index = len(model.bones.keys())
        model.bones[left_small_toe_bone.name] = left_small_toe_bone
        model.bone_indexes[left_small_toe_bone.index] = left_small_toe_bone.name

    return model


# 眉モーフ
def calc_eyebrow(fno: int, motion: VmdMotion, frame_joints: dict):
    left_eye_brow1 = get_vec2(frame_joints["faces"], "left_eye_brow1")
    left_eye_brow2 = get_vec2(frame_joints["faces"], "left_eye_brow2")
    left_eye_brow3 = get_vec2(frame_joints["faces"], "left_eye_brow3")
    left_eye_brow4 = get_vec2(frame_joints["faces"], "left_eye_brow4")
    left_eye_brow5 = get_vec2(frame_joints["faces"], "left_eye_brow5")

    left_eye1 = get_vec2(frame_joints["faces"], "left_eye1")
    left_eye2 = get_vec2(frame_joints["faces"], "left_eye2")
    left_eye3 = get_vec2(frame_joints["faces"], "left_eye3")
    left_eye4 = get_vec2(frame_joints["faces"], "left_eye4")
    left_eye5 = get_vec2(frame_joints["faces"], "left_eye5")
    left_eye6 = get_vec2(frame_joints["faces"], "left_eye6")

    right_eye_brow1 = get_vec2(frame_joints["faces"], "right_eye_brow1")
    right_eye_brow2 = get_vec2(frame_joints["faces"], "right_eye_brow2")
    right_eye_brow3 = get_vec2(frame_joints["faces"], "right_eye_brow3")
    right_eye_brow4 = get_vec2(frame_joints["faces"], "right_eye_brow4")
    right_eye_brow5 = get_vec2(frame_joints["faces"], "right_eye_brow5")

    right_eye1 = get_vec2(frame_joints["faces"], "right_eye1")
    right_eye2 = get_vec2(frame_joints["faces"], "right_eye2")
    right_eye3 = get_vec2(frame_joints["faces"], "right_eye3")
    right_eye4 = get_vec2(frame_joints["faces"], "right_eye4")
    right_eye5 = get_vec2(frame_joints["faces"], "right_eye5")
    right_eye6 = get_vec2(frame_joints["faces"], "right_eye6")

    left_nose_1 = get_vec2(frame_joints["faces"], 'left_nose_1')
    right_nose_2 = get_vec2(frame_joints["faces"], 'right_nose_2')

    # 鼻の幅
    nose_width = abs(left_nose_1.x() - right_nose_2.x())
    
    # 眉のしかめ具合
    frown_ratio = abs(left_eye_brow1.x() - right_eye_brow1.x()) / nose_width

    # 眉の幅
    eye_brow_length = (euclidean_distance(left_eye_brow1, left_eye_brow5) + euclidean_distance(right_eye_brow1, right_eye_brow5)) / 2
    # 目の幅
    eye_length = (euclidean_distance(left_eye1, left_eye4) + euclidean_distance(right_eye1, right_eye4)) / 2

    # 目と眉の縦幅
    left_vertical_length = (euclidean_distance(left_eye1, left_eye_brow1) + euclidean_distance(right_eye1, right_eye_brow1)) / 2
    center_vertical_length = (euclidean_distance(left_eye2, left_eye_brow3) + euclidean_distance(right_eye2, right_eye_brow3)) / 2
    right_vertical_length = (euclidean_distance(left_eye4, left_eye_brow5) + euclidean_distance(right_eye4, right_eye_brow5)) / 2

    left_ratio = left_vertical_length / eye_brow_length
    center_ratio = center_vertical_length / eye_brow_length
    right_ratio = right_vertical_length / eye_brow_length

    updown_ratio = center_ratio - 0.5

    if updown_ratio >= 0.2:
        # 上
        mf = VmdMorphFrame(fno)
        mf.set_name("上")
        mf.ratio = max(0, min(1, abs(updown_ratio) + 0.3))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("下")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)
    else:
        # 下
        mf = VmdMorphFrame(fno)
        mf.set_name("下")
        mf.ratio = max(0, min(1, abs(updown_ratio) + 0.3))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("上")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)
    
    mf = VmdMorphFrame(fno)
    mf.set_name("困る")
    mf.ratio = max(0, min(1, (0.8 - frown_ratio)))
    motion.regist_mf(mf, mf.name, mf.fno)

    if left_ratio >= right_ratio:
        # 怒る系
        mf = VmdMorphFrame(fno)
        mf.set_name("怒り")
        mf.ratio = max(0, min(1, abs(left_ratio - right_ratio)))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("にこり")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)
    else:
        # 笑う系
        mf = VmdMorphFrame(fno)
        mf.set_name("にこり")
        mf.ratio = max(0, min(1, abs(right_ratio - left_ratio)))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("怒り")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)

# リップモーフ
def calc_lip(fno: int, motion: VmdMotion, frame_joints: dict):
    left_nose_1 = get_vec2(frame_joints["faces"], 'left_nose_1')
    right_nose_2 = get_vec2(frame_joints["faces"], 'right_nose_2')

    left_mouth_1 = get_vec2(frame_joints["faces"], 'left_mouth_1')
    left_mouth_2 = get_vec2(frame_joints["faces"], 'left_mouth_2')
    left_mouth_3 = get_vec2(frame_joints["faces"], 'left_mouth_3')
    mouth_top = get_vec2(frame_joints["faces"], 'mouth_top')
    right_mouth_3 = get_vec2(frame_joints["faces"], 'right_mouth_3')
    right_mouth_2 = get_vec2(frame_joints["faces"], 'right_mouth_2')
    right_mouth_1 = get_vec2(frame_joints["faces"], 'right_mouth_1')
    right_mouth_5 = get_vec2(frame_joints["faces"], 'right_mouth_5')
    right_mouth_4 = get_vec2(frame_joints["faces"], 'right_mouth_4')
    mouth_bottom = get_vec2(frame_joints["faces"], 'mouth_bottom')
    left_mouth_4 = get_vec2(frame_joints["faces"], 'left_mouth_4')
    left_mouth_5 = get_vec2(frame_joints["faces"], 'left_mouth_5')
    left_lip_1 = get_vec2(frame_joints["faces"], 'left_lip_1')
    left_lip_2 = get_vec2(frame_joints["faces"], 'left_lip_2')
    lip_top = get_vec2(frame_joints["faces"], 'lip_top')
    right_lip_2 = get_vec2(frame_joints["faces"], 'right_lip_2')
    right_lip_1 = get_vec2(frame_joints["faces"], 'right_lip_1')
    right_lip_3 = get_vec2(frame_joints["faces"], 'right_lip_3')
    lip_bottom = get_vec2(frame_joints["faces"], 'lip_bottom')
    left_lip_3 = get_vec2(frame_joints["faces"], 'left_lip_3')

    # 鼻の幅
    nose_width = abs(left_nose_1.x() - right_nose_2.x())

    # 口角の平均値
    corner_center = (left_mouth_1 + right_mouth_1) / 2
    # 口角の幅
    mouse_width = abs(left_mouth_1.x() - right_mouth_1.x())

    # 鼻基準の口の横幅比率
    mouse_width_ratio = mouse_width / nose_width

    # 上唇の平均値
    top_mouth_center = (right_mouth_3 + mouth_top + left_mouth_3) / 3
    top_lip_center = (right_lip_2 + lip_top + left_lip_2) / 3

    # 下唇の平均値
    bottom_mouth_center = (right_mouth_4 + mouth_bottom + left_mouth_4) / 3
    bottom_lip_center = (right_lip_3 + lip_bottom + left_lip_3) / 3

    # 唇の外側の開き具合に対する内側の開き具合
    open_ratio = (bottom_lip_center.y() - top_lip_center.y()) / (bottom_mouth_center.y() - top_mouth_center.y())

    # 笑いの比率
    smile_ratio = (bottom_mouth_center.y() - corner_center.y()) / (bottom_mouth_center.y() - top_mouth_center.y())

    if smile_ratio >= 0:
        mf = VmdMorphFrame(fno)
        mf.set_name("∧")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("にやり")
        mf.ratio = max(0, min(1, abs(smile_ratio)))
        motion.regist_mf(mf, mf.name, mf.fno)
    else:
        mf = VmdMorphFrame(fno)
        mf.set_name("∧")
        mf.ratio = max(0, min(1, abs(smile_ratio)))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("にやり")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)

    if mouse_width_ratio > 1.3:
        # 横幅広がってる場合は「い」
        mf = VmdMorphFrame(fno)
        mf.set_name("い")
        mf.ratio = max(0, min(1, 1.5 / mouse_width_ratio) * open_ratio)
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("う")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("あ")
        mf.ratio = max(0, min(1 - min(0.7, smile_ratio), open_ratio))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("お")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)
    else:
        # 狭まっている場合は「う」
        mf = VmdMorphFrame(fno)
        mf.set_name("い")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("う")
        mf.ratio = max(0, min(1, (1.2 / mouse_width_ratio) * open_ratio))
        motion.regist_mf(mf, mf.name, mf.fno)

        mf = VmdMorphFrame(fno)
        mf.set_name("あ")
        mf.ratio = 0
        motion.regist_mf(mf, mf.name, mf.fno)        

        mf = VmdMorphFrame(fno)
        mf.set_name("お")
        mf.ratio = max(0, min(1 - min(0.7, smile_ratio), open_ratio))
        motion.regist_mf(mf, mf.name, mf.fno)

def blend_eye(fno: int, motion: VmdMotion, left_eye_euler: MVector3D, right_eye_euler: MVector3D):
    min_blink = min(motion.morphs["ウィンク右"][fno].ratio, motion.morphs["ウィンク"][fno].ratio)
    min_smile = min(motion.morphs["ｳｨﾝｸ２右"][fno].ratio, motion.morphs["ウィンク２"][fno].ratio)

    # 両方の同じ値はさっぴく
    motion.morphs["ウィンク右"][fno].ratio -= min_smile
    motion.morphs["ウィンク"][fno].ratio -= min_smile

    motion.morphs["ｳｨﾝｸ２右"][fno].ratio -= min_blink
    motion.morphs["ウィンク２"][fno].ratio -= min_blink

    for morph_name in ["ウィンク右", "ウィンク", "ｳｨﾝｸ２右", "ウィンク２"]:
        motion.morphs[morph_name][fno].ratio = max(0, min(1, motion.morphs[morph_name][fno].ratio))

    mf = VmdMorphFrame(fno)
    mf.set_name("笑い")
    mf.ratio = max(0, min(1, min_smile))
    motion.regist_mf(mf, mf.name, mf.fno)

    mf = VmdMorphFrame(fno)
    mf.set_name("まばたき")
    mf.ratio = max(0, min(1, min_blink))
    motion.regist_mf(mf, mf.name, mf.fno)

    # 両目の平均とする
    mean_eye_euler = (right_eye_euler + left_eye_euler) / 2
    eye_bf = motion.calc_bf("両目", fno)
    eye_bf.rotation = MQuaternion.fromEulerAngles(mean_eye_euler.x(), mean_eye_euler.y(), 0)
    motion.regist_bf(eye_bf, "両目", fno)
    

def calc_left_eye(fno: int, motion: VmdMotion, frame_joints: dict):
    left_eye1 = get_vec2(frame_joints["faces"], "left_eye1")
    left_eye2 = get_vec2(frame_joints["faces"], "left_eye2")
    left_eye3 = get_vec2(frame_joints["faces"], "left_eye3")
    left_eye4 = get_vec2(frame_joints["faces"], "left_eye4")
    left_eye5 = get_vec2(frame_joints["faces"], "left_eye5")
    left_eye6 = get_vec2(frame_joints["faces"], "left_eye6")

    if "eyes" in frame_joints:
        left_pupil = MVector2D(frame_joints["eyes"]["left"]["x"], frame_joints["eyes"]["left"]["y"])
    else:
        left_pupil = MVector2D()

    # 左目のEAR(eyes aspect ratio)
    left_blink, left_smile, left_eye_euler = get_blink_ratio(fno, left_eye1, left_eye2, left_eye3, left_eye4, left_eye5, left_eye6, left_pupil)

    mf = VmdMorphFrame(fno)
    mf.set_name("ウィンク右")
    mf.ratio = max(0, min(1, left_smile))

    motion.regist_mf(mf, mf.name, mf.fno)

    mf = VmdMorphFrame(fno)
    mf.set_name("ｳｨﾝｸ２右")
    mf.ratio = max(0, min(1, left_blink))

    motion.regist_mf(mf, mf.name, mf.fno)

    return left_eye_euler

def calc_right_eye(fno: int, motion: VmdMotion, frame_joints: dict):
    right_eye1 = get_vec2(frame_joints["faces"], "right_eye1")
    right_eye2 = get_vec2(frame_joints["faces"], "right_eye2")
    right_eye3 = get_vec2(frame_joints["faces"], "right_eye3")
    right_eye4 = get_vec2(frame_joints["faces"], "right_eye4")
    right_eye5 = get_vec2(frame_joints["faces"], "right_eye5")
    right_eye6 = get_vec2(frame_joints["faces"], "right_eye6")

    if "eyes" in frame_joints:
        right_pupil = MVector2D(frame_joints["eyes"]["right"]["x"], frame_joints["eyes"]["right"]["y"])
    else:
        right_pupil = MVector2D()

    # 右目のEAR(eyes aspect ratio)
    right_blink, right_smile, right_eye_euler = get_blink_ratio(fno, right_eye1, right_eye2, right_eye3, right_eye4, right_eye5, right_eye6, right_pupil, is_right=True)

    mf = VmdMorphFrame(fno)
    mf.set_name("ウィンク")
    mf.ratio = max(0, min(1, right_smile))
    motion.regist_mf(mf, mf.name, mf.fno)

    mf = VmdMorphFrame(fno)
    mf.set_name("ウィンク２")
    mf.ratio = max(0, min(1, right_blink))
    motion.regist_mf(mf, mf.name, mf.fno)

    return right_eye_euler

def euclidean_distance(point1: MVector2D, point2: MVector2D):
    return math.sqrt((point1.x() - point2.x())**2 + (point1.y() - point2.y())**2)

def get_blink_ratio(fno: int, eye1: MVector2D, eye2: MVector2D, eye3: MVector2D, eye4: MVector2D, eye5: MVector2D, eye6: MVector2D, pupil: MVector2D, is_right=False):
    #loading all the required points
    corner_left  = eye4
    corner_right = eye1
    corner_center = (eye1 + eye4) / 2
    
    center_top = (eye2 + eye3) / 2
    center_bottom = (eye5 + eye6) / 2

    #calculating distance
    horizontal_length = euclidean_distance(corner_left, corner_right)
    vertical_length = euclidean_distance(center_top, center_bottom)

    ratio = horizontal_length / vertical_length
    new_ratio = min(1, math.sin(calc_ratio(ratio, 0, 12, 0, 1)))

    # 笑いの比率(日本人用に比率半分)
    smile_ratio = (center_bottom.y() - corner_center.y()) / (center_bottom.y() - center_top.y())

    if smile_ratio > 1:
        # １より大きい場合、目頭よりも上瞼が下にあるという事なので、通常瞬きと見なす
        return 1, 0, MVector3D()
    
    # 目は四角の中にあるはず
    pupil_x = 0
    pupil_y = 0
    if pupil != MVector2D():
        eye_right = ((eye3 + eye5) / 2)
        eye_left = ((eye2 + eye6) / 2)
        pupil_horizonal_ratio = (pupil.x() - min(eye1.x(), eye4.x())) / (max(eye1.x(), eye4.x()) - min(eye1.x(), eye4.x()))
        pupil_vertical_ratio = (pupil.y() - center_top.y()) / (center_bottom.y() - center_top.y())

        if abs(pupil_horizonal_ratio) <= 1 and abs(pupil_vertical_ratio) <= 1:
            # 比率を超えている場合、計測失敗してるので初期値            
            pupil_x = calc_ratio(pupil_vertical_ratio, 0, 1, -15, 15)
            pupil_y = calc_ratio(pupil_horizonal_ratio, 0, 1, -30, 20)

    return new_ratio * (1 - smile_ratio), new_ratio * smile_ratio, MVector3D(pupil_x * -1, pupil_y * -1, 0)

def calc_ratio(ratio: float, oldmin: float, oldmax: float, newmin: float, newmax: float):
    # https://qastack.jp/programming/929103/convert-a-number-range-to-another-range-maintaining-ratio
    # NewValue = (((OldValue - OldMin) * (NewMax - NewMin)) / (OldMax - OldMin)) + NewMin
    return (((ratio - oldmin) * (newmax - newmin)) / (oldmax - oldmin)) + newmin

def get_vec2(joints: dict, jname: str):
    if jname in MORPH_CONNECTIONS:
        joint = joints[MORPH_CONNECTIONS[jname]]
        return MVector2D(joint["x"], joint["y"])
    
    return MVector2D()


MORPH_CONNECTIONS = {
    # Left Eye brow
    'left_eye_brow1': "23",
    'left_eye_brow2': "24",
    'left_eye_brow3': "25",
    'left_eye_brow4': "26",
    'left_eye_brow5': "27",

    # Right Eye brow
    'right_eye_brow1': "22",
    'right_eye_brow2': "21",
    'right_eye_brow3': "20",
    'right_eye_brow4': "19",
    'right_eye_brow5': "18",

    # Left Eye
    'left_eye1': "46",
    'left_eye2': "45",
    'left_eye3': "44",
    'left_eye4': "43",
    'left_eye5': "48",
    'left_eye6': "47",

    # Right Eye
    'right_eye1': "40",
    'right_eye2': "39",
    'right_eye3': "38",
    'right_eye4': "37",
    'right_eye5': "42",
    'right_eye6': "41",

    # Nose Vertical
    'nose1': "28",
    'nose2': "29",
    'nose3': "30",
    'nose4': "31",

    # Nose Horizontal
    'left_nose_1': "36",
    'left_nose_2': "35",
    'nose_middle': "34",
    'right_nose_1': "33",
    'right_nose_2': "32",

    # Mouth
    'left_mouth_1': "55",
    'left_mouth_2': "54",
    'left_mouth_3': "53",
    'mouth_top': "52",
    'right_mouth_3': "51",
    'right_mouth_2': "50",
    'right_mouth_1': "49",
    'right_mouth_5': "60",
    'right_mouth_4': "59",
    'mouth_bottom': "58",
    'left_mouth_4': "57",
    'left_mouth_5': "56",

    # Lips
    'left_lip_1': "65",
    'left_lip_2': "64",
    'lip_top': "63",
    'right_lip_2': "62",
    'right_lip_1': "61",
    'right_lip_3': "68",
    'lip_bottom': "67",
    'left_lip_3': "66",

}


VMD_CONNECTIONS = {
    'spine1': ("上半身", ['pelvis', 'spine1', 'left_shoulder', 'right_shoulder', 'pelvis', 'spine1'], [], None, False, False),
    'spine2': ("上半身2", ['spine1', 'spine2', 'left_shoulder', 'right_shoulder', 'spine1', 'spine2'], ["上半身"], None, False, False),
    'spine3': ("首根元", None, None, None, False, False),
    'neck': ("首", ['neck', 'nose', 'left_eye', 'right_eye', 'neck', 'nose'], ["上半身", "上半身2"], None, False, True),
    'head': ("頭", ['nose', 'head', 'left_eye', 'right_eye', 'nose', 'head'], ["上半身", "上半身2", "首"], None, False, True),
    'pelvis': ("下半身", None, None, None, False, False),
    'nose': ("鼻", None, None, None, False, False),
    'right_eye': ("右目", None, None, None, False, False),
    'left_eye': ("左目", None, None, None, False, False),
    'right_ear': ("右耳", None, None, None, False, False),
    'left_ear': ("左耳", None, None, None, False, False),
    'right_shoulder': ("右肩", ['spine3', 'right_shoulder', 'spine1', 'spine3', 'right_shoulder', 'left_shoulder'], ["上半身", "上半身2"], None, False, False),
    'left_shoulder': ("左肩", ['spine3', 'left_shoulder', 'spine1', 'spine3', 'left_shoulder', 'right_shoulder'], ["上半身", "上半身2"], None, False, False),
    'right_arm': ("右腕", ['right_shoulder', 'right_elbow', 'spine3', 'right_shoulder', 'right_shoulder', 'right_elbow'], ["上半身", "上半身2", "右肩"], None, False, False),
    'left_arm': ("左腕", ['left_shoulder', 'left_elbow', 'spine3', 'left_shoulder', 'left_shoulder', 'left_elbow'], ["上半身", "上半身2", "左肩"], None, False, False),
    'right_elbow': ("右ひじ", ['right_elbow', 'right_wrist', 'spine3', 'right_shoulder', 'right_elbow', 'right_wrist'], ["上半身", "上半身2", "右肩", "右腕"], None, False, False),
    'left_elbow': ("左ひじ", ['left_elbow', 'left_wrist', 'spine3', 'left_shoulder', 'left_elbow', 'left_wrist'], ["上半身", "上半身2", "左肩", "左腕"], None, False, False),
    'right_wrist': ("右手首", ['right_wrist', 'right_middle1', 'right_index1', 'right_pinky1', 'right_wrist', 'right_middle1'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ"], None, True, False),
    'left_wrist': ("左手首", ['left_wrist', 'left_middle1', 'left_index1', 'left_pinky1', 'left_wrist', 'left_middle1'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ"], None, True, False),
    'pelvis': ("下半身", ['spine1', 'pelvis', 'left_hip', 'right_hip', 'pelvis', 'pelvis2'], [], None, False, False),
    'pelvis2': ("尾てい骨", None, None, None, False, False),
    'right_hip': ("右足", ['right_hip', 'right_knee', 'pelvis2', 'right_hip', 'right_hip', 'right_knee'], ["下半身"], None, False, False),
    'left_hip': ("左足", ['left_hip', 'left_knee', 'pelvis2', 'left_hip', 'left_hip', 'left_knee'], ["下半身"], None, False, False),
    'right_knee': ("右ひざ", ['right_knee', 'right_ankle', 'pelvis2', 'right_hip', 'right_knee', 'right_ankle'], ["下半身", "右足"], None, False, False),
    'left_knee': ("左ひざ", ['left_knee', 'left_ankle', 'pelvis2', 'left_hip', 'left_knee', 'left_ankle'], ["下半身", "左足"], None, False, False),
    'right_ankle': ("右足首", ['right_ankle', 'right_big_toe', 'right_big_toe', 'right_small_toe', 'right_ankle', 'right_big_toe'], ["下半身", "右足", "右ひざ"], None, False, False),
    'left_ankle': ("左足首", ['left_ankle', 'left_big_toe', 'left_big_toe', 'left_small_toe', 'left_ankle', 'left_big_toe'], ["下半身", "左足", "左ひざ"], None, False, False),
    'right_big_toe': ("右足親指", None, None, None, False, False),
    'right_small_toe': ("右足小指", None, None, None, False, False),
    'left_big_toe': ("左足親指", None, None, None, False, False),
    'left_small_toe': ("左足小指", None, None, None, False, False),
    'right_thumb1': ("右親指０", ['right_thumb1', 'right_thumb2', 'right_thumb1', 'right_pinky1', 'right_thumb1', 'right_thumb2'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ", "右手首"], None, True, False),
    'left_thumb1': ("左親指０", ['left_thumb1', 'left_thumb2', 'left_thumb1', 'left_pinky1', 'left_thumb1', 'left_thumb2'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ", "左手首"], None, True, False),
    'right_thumb2': ("右親指１", ['right_thumb2', 'right_thumb3', 'right_thumb1', 'right_pinky1', 'right_thumb2', 'right_thumb3'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右親指０"], None, True, False),
    'left_thumb2': ("左親指１", ['left_thumb2', 'left_thumb3', 'left_thumb1', 'left_pinky1', 'left_thumb2', 'left_thumb3'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左親指０"], None, True, False),
    'right_thumb3': ("右親指２", ['right_thumb3', 'right_thumb', 'right_thumb1', 'right_pinky1', 'right_thumb3', 'right_thumb'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右親指０", "右親指１"], None, True, False),
    'left_thumb3': ("左親指２", ['left_thumb3', 'left_thumb', 'left_thumb1', 'left_pinky1', 'left_thumb3', 'left_thumb'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左親指０", "左親指１"], None, True, False),
    'right_thumb': ("右親差指先", None, None, None, True, False),
    'left_thumb': ("左親差指先", None, None, None, True, False),
    'right_index1': ("右人指１", ['right_index1', 'right_index2', 'right_index1', 'right_pinky1', 'right_index1', 'right_index2'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ", "右手首"], None, True, False),
    'left_index1': ("左人指１", ['left_index1', 'left_index2', 'left_index1', 'left_pinky1', 'left_index1', 'left_index2'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ", "左手首"], None, True, False),
    'right_index2': ("右人指２", ['right_index2', 'right_index3', 'right_index1', 'right_pinky1', 'right_index2', 'right_index3'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右人指１"], None, True, False),
    'left_index2': ("左人指２", ['left_index2', 'left_index3', 'left_index1', 'left_pinky1', 'left_index2', 'left_index3'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左人指１"], None, True, False),
    'right_index3': ("右人指３", ['right_index3', 'right_index', 'right_index1', 'right_pinky1', 'right_index3', 'right_index'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右人指１", "右人指２"], None, True, False),
    'left_index3': ("左人指３", ['left_index3', 'left_index', 'left_index1', 'left_pinky1', 'left_index3', 'left_index'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左人指１", "左人指２"], None, True, False),
    'right_index': ("右人差指先", None, None, None, True, False),
    'left_index': ("左人差指先", None, None, None, True, False),
    'right_middle1': ("右中指１", ['right_middle1', 'right_middle2', 'right_index1', 'right_pinky1', 'right_middle1', 'right_middle2'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ", "右手首"], None, True, False),
    'left_middle1': ("左中指１", ['left_middle1', 'left_middle2', 'left_index1', 'left_pinky1', 'right_middle1', 'left_middle2'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ", "左手首"], None, True, False),
    'right_middle2': ("右中指２", ['right_middle2', 'right_middle3', 'right_index1', 'right_pinky1', 'right_middle2', 'right_middle3'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右中指１"], None, True, False),
    'left_middle2': ("左中指２", ['left_middle2', 'left_middle3', 'left_index1', 'left_pinky1', 'left_middle2', 'left_middle3'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左中指１"], None, True, False),
    'right_middle3': ("右中指３", ['right_middle3', 'right_middle', 'right_index1', 'right_pinky1', 'right_middle3', 'right_middle'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右中指１", "右中指２"], None, True, False),
    'left_middle3': ("左中指３", ['left_middle3', 'left_middle', 'left_index1', 'left_pinky1', 'left_middle3', 'left_middle'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左中指１", "左中指２"], None, True, False),
    'right_middle': ("右中指先", None, None, None, True, False),
    'left_middle': ("左中指先", None, None, None, True, False),
    'right_ring1': ("右薬指１", ['right_ring1', 'right_ring2', 'right_index1', 'right_pinky1', 'right_ring1', 'right_ring2'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ", "右手首"], None, True, False),
    'left_ring1': ("左薬指１", ['left_ring1', 'left_ring2', 'left_index1', 'left_pinky1', 'left_ring1', 'left_ring2'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ", "左手首"], None, True, False),
    'right_ring2': ("右薬指２", ['right_ring2', 'right_ring3', 'right_index1', 'right_pinky1', 'right_ring2', 'right_ring3'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右薬指１"], None, True, False),
    'left_ring2': ("左薬指２", ['left_ring2', 'left_ring3', 'left_index1', 'left_pinky1', 'left_ring2', 'left_ring3'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左薬指１"], None, True, False),
    'right_ring3': ("右薬指３", ['right_ring3', 'right_ring', 'right_index1', 'right_pinky1', 'right_ring3', 'right_ring'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右薬指１", "右薬指２"], None, True, False),
    'left_ring3': ("左薬指３", ['left_ring3', 'left_ring', 'left_index1', 'left_pinky1', 'left_ring3', 'left_ring'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左薬指１", "左薬指２"], None, True, False),
    'right_ring': ("右薬指先", None, None, None, True, False),
    'left_ring': ("左薬指先", None, None, None, True, False),
    'right_pinky1': ("右小指１", ['right_pinky1', 'right_pinky2', 'right_index1', 'right_pinky1', 'right_pinky1', 'right_pinky2'], ["上半身", "上半身2", "右肩", "右腕", "右ひじ", "右手首"], None, True, False),
    'left_pinky1': ("左小指１", ['left_pinky1', 'left_pinky2', 'left_index1', 'left_pinky1', 'left_pinky1', 'left_pinky2'], ["上半身", "上半身2", "左肩", "左腕", "左ひじ", "左手首"], None, True, False),
    'right_pinky2': ("右小指２", ['right_pinky2', 'right_pinky3', 'right_index1', 'right_pinky1', 'right_pinky2', 'right_pinky3'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右小指１"], None, True, False),
    'left_pinky2': ("左小指２", ['left_pinky2', 'left_pinky3', 'left_index1', 'left_pinky1', 'left_pinky2', 'left_pinky3'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左小指１"], None, True, False),
    'right_pinky3': ("右小指３", ['right_pinky3', 'right_pinky', 'right_index1', 'right_pinky1', 'right_pinky3', 'right_pinky'], ["上半身", "上半身3", "右肩", "右腕", "右ひじ", "右手首", "右小指１", "右小指２"], None, True, False),
    'left_pinky3': ("左小指３", ['left_pinky3', 'left_pinky', 'left_index1', 'left_pinky1', 'left_pinky3', 'left_pinky'], ["上半身", "上半身3", "左肩", "左腕", "左ひじ", "左手首", "左小指１", "左小指２"], None, True, False),
    'right_pinky': ("右小指先", None, None, None, True, False),
    'left_pinky': ("左小指先", None, None, None, True, False),
}
