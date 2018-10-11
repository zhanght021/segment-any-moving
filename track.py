"""Playground for tracking objects."""

import argparse
import collections
import logging
import os
import pickle
import pprint
from pathlib import Path

import cv2
import numpy as np
import PIL
import pycocotools.mask as mask_util
import skimage.color
from tqdm import tqdm
from moviepy.editor import ImageSequenceClip
from scipy.spatial.distance import cosine

import utils.vis as vis
from utils.colors import colormap
from utils.datasets import get_classes
from utils.distance import chi_square_distance  # , intersection_distance
from utils.log import setup_logging


def decay_weighted_mean(values, sigma=5):
    """Weighted mean that focuses on most recent values.

    values[-i] is weighted by np.exp(i / sigma). Higher sigma weights older
    values more heavily.

    values (array-like): Values arranged from oldest to newest. values[-1] is
        weighted most heavily, values[0] is weighted least.
    """
    weights = np.exp([-i / sigma for i in range(len(values))])[::-1]
    return np.average(values, weights=weights)


class Detection():
    def __init__(self,
                 box,
                 score,
                 label,
                 timestamp,
                 image=None,
                 mask=None,
                 mask_feature=None):
        self.box = box  # (x1, y1, x2, y2)
        self.score = score
        self.label = label
        self.timestamp = timestamp
        self.image = image
        self.mask = mask
        self.mask_feature = mask_feature
        self.track = None
        self._cached_values = {
            'contour_moments': None,
            'center_box': None,
            'center_mask': None,
            'decoded_mask': None,
            'mask_histogram': None,
            'mask_histogram_edges': None
        }

    def clear_cache(self):
        for key in self._cached_values:
            self._cached_values[key] = None

    def contour_moments(self):
        if self._cached_values['contour_moments'] is None:
            self._cached_values['contour_moments'] = cv2.moments(
                self.decoded_mask())
        return self._cached_values['contour_moments']

    def compute_center(self):
        if self._cached_values['center_mask'] is None:
            moments = self.contour_moments()
            # See
            # <https://docs.opencv.org/3.1.0/dd/d49/tutorial_py_contour_features.html>
            cx = int(moments['m10'] / moments['m00'])
            cy = int(moments['m01'] / moments['m00'])
            self._cached_values['center_mask'] = (cx, cy)
        return self._cached_values['center_mask']

    def compute_center_box(self):
        if self._cached_values['center_box'] is None:
            x0, y0, x1, y1 = self.box
            self._cached_values['center_box'] = ((x0 + x1) / 2, (y0 + y1) / 2)
        return self._cached_values['center_box']

    def compute_area(self):
        return self.contour_moments()['m00']

    def compute_area_bbox(self):
        x0, y0, x1, y1 = self.box
        return (x1 - x0) * (y1 - y0)

    def decoded_mask(self):
        if self._cached_values['decoded_mask'] is None:
            self._cached_values['decoded_mask'] = mask_util.decode(self.mask)
        return self._cached_values['decoded_mask']

    def bbox_centered_mask(self):
        """Return mask with respect to bounding box coordinates."""
        x1, y1, x2, y2 = self.box
        x1, y1, x2, y2 = int(x1-0.5), int(y1-0.5), int(x2+0.5), int(y2+0.5)
        return self.decoded_mask()[x1:x2+1, y1:y2+1]

    def centered_mask_iou(self, detection):
        mask = self.bbox_centered_mask()
        other_mask = detection.bbox_centered_mask()
        if mask.size == 0 or other_mask.size == 0:
            return 0
        if mask.shape != other_mask.shape:
            mask_image = PIL.Image.fromarray(mask)
            other_mask_image = PIL.Image.fromarray(other_mask)
            if mask.size > other_mask.size:
                other_mask_image = other_mask_image.resize(
                    mask_image.size, resample=PIL.Image.NEAREST)
            else:
                mask_image = mask_image.resize(
                    other_mask_image.size, resample=PIL.Image.NEAREST)
            mask = np.array(mask_image)
            other_mask = np.array(other_mask_image)
        intersection = (mask & other_mask).sum()
        union = (mask | other_mask).sum()
        if union == 0:
            return 0
        else:
            return intersection / union

    def mask_iou(self, detection):
        return mask_util.iou(
            [self.mask], [detection.mask], pyiscrowd=np.zeros(1)).item()

    def compute_histogram(self):
        # Compute histogram in LAB space, as in
        #     Xiao, Jianxiong, et al. "Sun database: Large-scale scene
        #     recognition from abbey to zoo." Computer vision and pattern
        #     recognition (CVPR), 2010 IEEE conference on. IEEE, 2010.
        # https://www.cc.gatech.edu/~hays/papers/sun.pdf
        if self._cached_values['mask_histogram'] is not None:
            return (self._cached_values['mask_histogram'],
                    self._cached_values['mask_histogram_edges'])
        # (num_pixels, num_channels)
        mask_pixels = self.image[np.nonzero(self.decoded_mask())]
        # rgb2lab expects a 3D tensor with colors in the last dimension, so
        # just add a fake first dimension.
        mask_pixels = mask_pixels[np.newaxis]
        mask_pixels = skimage.color.rgb2lab(mask_pixels)[0]
        # TODO(achald): Check if the range for LAB is correct.
        mask_histogram, mask_histogram_edges = np.histogramdd(
            mask_pixels,
            bins=[4, 14, 14],
            range=[[0, 100], [-127, 128], [-127, 128]])
        normalizer = mask_histogram.sum()
        if normalizer == 0:
            normalizer = 1
        mask_histogram /= normalizer
        self._cached_values['mask_histogram'] = mask_histogram
        self._cached_values['mask_histogram_edges'] = mask_histogram_edges
        return (self._cached_values['mask_histogram'],
                self._cached_values['mask_histogram_edges'])


class Track():
    def __init__(self, track_id):
        self.detections = []
        self.id = track_id
        self.velocity = None

    def add_detection(self, detection, timestamp):
        detection.track = self
        self.detections.append(detection)

    def last_timestamp(self):
        if self.detections:
            return self.detections[-1].timestamp
        else:
            return None


def track_distance(track, detection):
    # if len(track.detections) > 2:
    #     history = min(len(track.detections) - 1, 5)
    #     centers = np.asarray(
    #         [x.compute_center_box() for x in track.detections[-history:]])
    #     velocities = centers[1:] - centers[:-1]
    #     predicted_box = track.detections[-1].box
    #     if np.all(
    #             np.std(velocities, axis=0) < 0.1 *
    #             track.detections[-1].compute_area_bbox()):
    #         track.velocity = np.mean(velocities, axis=0)
    #         predicted_center = centers[-1] + track.velocity
    #     else:
    #         predicted_center = centers[-1]
    # else:
    #     predicted_box = track.detections[-1].box
    #     predicted_center = track.detections[-1].compute_center_box()
    # area = decay_weighted_mean(
    #     [x.compute_area_bbox() for x in track.detections])
    # target_area = detection.compute_area_bbox()
    # return (max([abs(p1 - p0)
    #              for p0, p1 in zip(predicted_box, detection.box)]) / area)
    predicted_cx, predicted_cy = track.detections[-1].compute_center_box()
    detection_cx, detection_cy = detection.compute_center_box()
    diff_norm = ((predicted_cx - detection_cx)**2 +
                 (predicted_cy - detection_cy)**2)**0.5
    diagonal = (detection.image.shape[0]**2 + detection.image.shape[1]**2)**0.5
    return (diff_norm / diagonal)


def _match_detections_single_timestep(tracks, detections, tracking_params):
    matched_tracks = [None for _ in detections]

    sorted_indices = sorted(
        range(len(detections)),
        key=lambda index: detections[index].score,
        reverse=True)

    candidates = {track.id: sorted_indices.copy() for track in tracks}

    # Stage 1: Keep candidates with similar areas only.
    for track in tracks:
        track_area = track.detections[-1].compute_area()
        if track_area == 0:
            candidates[track.id] = 0
            continue
        new_candidates = []
        for i in candidates[track.id]:
            d = detections[i]
            area = d.compute_area()
            if (area > 0
                    and (area / track_area) > tracking_params['area_ratio']
                    and (track_area / area) > tracking_params['area_ratio']):
                new_candidates.append(i)
        candidates[track.id] = new_candidates

    # Stage 2: Match tracks to detections with good mask IOU.
    for track in tracks:
        track_ious = {}
        track_detection = track.detections[-1]
        for i in candidates[track.id]:
            already_matched = matched_tracks[i] is not None
            if tracking_params['ignore_labels']:
                different_label = False
            else:
                different_label = track_detection.label != detections[i].label
            too_far = (track_distance(track, detections[i]) >
                       tracking_params['spatial_dist_max'])
            if already_matched or different_label or too_far:
                continue
            track_ious[i] = track_detection.mask_iou(detections[i])
        if not track_ious:
            continue

        sorted_ious = sorted(
            track_ious.items(), key=lambda x: x[1], reverse=True)
        best_index, best_iou = sorted_ious[0]
        second_best_iou = sorted_ious[1][1] if len(sorted_ious) > 1 else 0
        if (best_iou - second_best_iou) > tracking_params['iou_gap_min']:
            matched_tracks[best_index] = track
            candidates[track.id] = []
        else:
            candidates[track.id] = [
                d for d, iou in sorted_ious
                if iou >= tracking_params['iou_min']
            ]

    # Stage 3: Match tracks to detections with good appearance threshold.
    for track in tracks:
        candidates[track.id] = [
            i for i in candidates[track.id] if matched_tracks[i] is None
        ]
        if not candidates[track.id]:
            continue
        appearance_distances = {}
        track_detection = track.detections[-1]
        for i in candidates[track.id]:
            if tracking_params['appearance_feature'] == 'mask':
                appearance_distances[i] = cosine(track_detection.mask_feature,
                                                 detections[i].mask_feature)
            elif tracking_params['appearance_feature'] == 'histogram':
                appearance_distances[i] = chi_square_distance(
                        track_detection.compute_histogram()[0],
                        detections[i].compute_histogram()[0])
        sorted_distances = sorted(
            appearance_distances.items(), key=lambda x: x[1])
        best_match, best_distance = sorted_distances[0]
        second_best_distance = (sorted_distances[1][1] if
                                len(sorted_distances) > 1 else np.float('inf'))
        if ((second_best_distance - best_distance) >
                tracking_params['appearance_gap']):
            matched_tracks[best_match] = track
            candidates[track.id] = []
    return matched_tracks


def match_detections(tracks, detections, tracking_params):
    """
    Args:
        track (list): List of Track objects, containing tracks up to this
            frame.
        detections (list): List of Detection objects for the current frame.

    Returns:
        matched_tracks (list): List of Track objects or None, of length
            len(detection), containing the Track, if any, that each Detection
            is assigned to.
    """
    # Tracks sorted by most recent to oldest.
    tracks_by_timestamp = collections.defaultdict(list)
    for track in tracks:
        tracks_by_timestamp[track.last_timestamp()].append(track)

    timestamps = sorted(tracks_by_timestamp.keys(), reverse=True)
    matched_tracks = [None for _ in detections]

    # Match detections to the most recent tracks first.
    unmatched_detection_indices = list(range(len(detections)))
    for timestamp in timestamps:
        single_timestamp_matched_tracks = _match_detections_single_timestep(
            tracks_by_timestamp[timestamp],
            [detections[i] for i in unmatched_detection_indices],
            tracking_params)
        new_unmatched_indices = []
        for i, track in enumerate(single_timestamp_matched_tracks):
            detection_index = unmatched_detection_indices[i]
            if track is not None:
                assert matched_tracks[detection_index] is None
                matched_tracks[detection_index] = track
            else:
                new_unmatched_indices.append(detection_index)
        unmatched_detection_indices = new_unmatched_indices

    return matched_tracks


def visualize_detections(image,
                         detections,
                         dataset,
                         tracking_params,
                         box_alpha=0.7,
                         dpi=200):
    if not detections:
        return image
    label_list = get_classes(dataset)

    # Display in largest to smallest order to reduce occlusion
    boxes = np.array([x.box for x in detections])
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    sorted_inds = np.argsort(-areas)

    colors = colormap()

    image = image.astype(dtype=np.uint8)
    for i in sorted_inds:
        detection = detections[i]
        color = [int(x) for x in colors[detection.track.id % len(colors), :3]]

        x0, y0, x1, y1 = [int(x) for x in detection.box]
        cx, cy = detection.compute_center_box()
        image = vis.vis_bbox(image, (x0, y0, x1 - x0, y1 - y0), color, thick=1)
        # image = vis.vis_bbox(image, (cx - 2, cy - 2, 2, 2), color, thick=3)

        # Draw spatial distance threshold
        if tracking_params['draw_spatial_threshold']:
            diagonal = (
                detection.image.shape[0]**2 + detection.image.shape[1]**2)**0.5
            cv2.circle(
                image, (int(cx), int(cy)),
                radius=int(diagonal * tracking_params['spatial_dist_max']),
                thickness=1,
                color=color)

        # if detection.track.velocity is not None:
        #     vx, vy = detection.track.velocity
        #     # Expand for visualization
        #     vx *= 2
        #     vy *= 2
        #     logging.info(
        #         'Drawing velocity at %s' % ((cx, cy, cx + vx, cy + vy), ))
        #     cv2.arrowedLine(
        #         image, (int(cx), int(cy)), (int(cx + vx), int(cy + vy)),
        #         color=color,
        #         thickness=3,
        #         tipLength=1.0)
        # else:
        #     cv2.circle(
        #         image, (int(cx), int(cy)),
        #         radius=3,
        #         thickness=1,
        #         color=color)
        image = vis.vis_mask(
            image,
            detection.decoded_mask(),
            color=color,
            alpha=0.1,
            border_thick=3)

        label_str = '({track}) {label}: {score}'.format(
            track=detection.track.id,
            label=label_list[detection.label],
            score='{:0.2f}'.format(detection.score).lstrip('0'))
        image = vis.vis_class(image, (x0, y0 - 2), label_str)

    return image


def track(frame_paths,
          frame_detections,
          tracking_params,
          progress=True,
          filter_label=None):
    """
    Args:
        frame_paths (list): List of paths to frames.
        frame_detections (list): List of detection results for each frame. Each
            element is a dictionary containing keys 'boxes', 'masks',
            and 'keypoints'.
        tracking_params (dict)
        label_list (list): List of label names.
        filter_label (str):
    """
    all_tracks = []
    current_tracks = []
    track_id = 0
    progress = tqdm(total=len(frame_paths), disable=not progress, desc='track')
    for timestamp, (image_path, image_results) in enumerate(
            zip(frame_paths, frame_detections)):
        if timestamp > 0:
            progress.update()
        for track in current_tracks:
            for detection in track.detections:
                detection.clear_cache()

        image = cv2.imread(str(image_path))[:, :, ::-1]  # BGR -> RGB

        boxes, masks, _, labels = vis.convert_from_cls_format(
            image_results['boxes'], image_results['segmentations'],
            image_results['keypoints'])

        if boxes is None:
            logging.info('No predictions for image %s', image_path.name)
            boxes, masks = [], []

        if ('features' in image_results
                and tracking_params['appearance_feature'] == 'mask'):
            # features are of shape (num_segments, d)
            mask_features = list(image_results['features'])
        else:
            mask_features = [None for _ in masks]

        detections = [
            Detection(box[:4], box[4], label, timestamp, image, mask,
                      feature) for box, mask, label, feature in zip(
                          boxes, masks, labels, mask_features)
            if (box[4] > tracking_params['score_continue_min'] and (
                filter_label is None or label == filter_label))
        ]
        matched_tracks = match_detections(current_tracks, detections,
                                          tracking_params)

        continued_tracks = []
        for detection, track in zip(detections, matched_tracks):
            if track is None:
                if detection.score > tracking_params['score_init_min']:
                    track = Track(track_id)
                    all_tracks.append(track)
                    track_id += 1
                else:
                    continue

            track.add_detection(detection, timestamp)
            continued_tracks.append(track)

        continued_track_ids = set([x.id for x in continued_tracks])
        skipped_tracks = []
        for track in current_tracks:
            if (track.id not in continued_track_ids
                    and (track.last_timestamp() - timestamp) <
                    tracking_params['frames_skip_max']):
                skipped_tracks.append(track)

        current_tracks = continued_tracks + skipped_tracks
    return all_tracks


def output_mot_tracks(tracks, label_list, frame_numbers, output_track_file):
    filtered_tracks = []
    for track in tracks:
        is_person = label_list[track.detections[-1].label] == 'person'
        is_long_enough = len(track.detections) > 4
        has_high_score = any(x.score >= 0.5 for x in track.detections)
        if is_person and is_long_enough and has_high_score:
            filtered_tracks.append(track)

    # Map frame number to list of Detections
    detections_by_frame = collections.defaultdict(list)
    for track in filtered_tracks:
        for detection in track.detections:
            detections_by_frame[detection.timestamp].append(detection)
    assert len(detections_by_frame) > 0

    output_str = ''
    # The last three fields are 'x', 'y', and 'z', and are only used for
    # 3D object detection.
    output_line_format = (
        '{frame},{track_id},{left},{top},{width},{height},{conf},-1,-1,-1'
        '\n')
    for timestamp, frame_detections in sorted(
            detections_by_frame.items(), key=lambda x: x[0]):
        for detection in frame_detections:
            x0, y0, x1, y1 = detection.box
            width = x1 - x0
            height = y1 - y0
            output_str += output_line_format.format(
                frame=frame_numbers[timestamp],
                track_id=detection.track.id,
                left=x0,
                top=y0,
                width=width,
                height=height,
                conf=detection.score,
                x=-1,
                y=-1,
                z=-1)
    with open(output_track_file, 'w') as f:
        f.write(output_str)


def visualize_tracks(tracks,
                     frame_paths,
                     dataset,
                     tracking_params,
                     output_dir=None,
                     output_video=None,
                     output_video_fps=10,
                     progress=False):
    """
    Args:
        tracks (list): List of Track objects
        frame_paths (list): List of frame paths.
        dataset (str): Dataset for utils/vis.py
        tracking_params (dict): Tracking parameters.
    """
    # Map frame number to list of Detections
    detections_by_frame = collections.defaultdict(list)
    for track in tracks:
        for detection in track.detections:
            detections_by_frame[detection.timestamp].append(detection)

    if output_video is not None:
        images = []
    for timestamp, image_path in enumerate(
            tqdm(frame_paths, disable=not progress)):
        image = cv2.imread(str(image_path))
        image = image[:, :, ::-1]  # BGR -> RGB
        new_image = visualize_detections(
            image,
            detections_by_frame[timestamp],
            dataset=dataset,
            tracking_params=tracking_params)

        if output_video is not None:
            images.append(new_image)

        if output_dir is not None:
            new_image = PIL.Image.fromarray(new_image)
            new_image.save(output_dir / (image_path.name + '.png'))

    if output_video is not None:
        clip = ImageSequenceClip(images, fps=output_video_fps)
        clip.write_videofile(str(output_video), verbose=progress)


def create_tracking_parser():
    """Create a parser with tracking arguments.

    Usage:
        tracking_parser = create_tracking_parser()

        parser = argparse.ArgumentParser(parents=[tracking_parser])

        tracking_params, remaining_argv = tracking_parser.parse_known_args()
        args = parser.parse_args(remaining_argv)

    Note that if add_tracking_params adds any required arguments, then the
    above code will cause --help to fail. Since the `tracking_parser` returned
    from this function does not implement --help, it will try to parse the
    known arguments even if --help is specified, and fail when a required
    argument is missing.
    """
    parent_parser = argparse.ArgumentParser(add_help=False)
    add_tracking_arguments(parent_parser)
    return parent_parser


def add_tracking_arguments(root_parser):
    """Add tracking params to an argument parser.  """
    parser = root_parser.add_argument_group('Tracking params')
    parser.add_argument(
        '--score-init-min',
        default=0.9,
        type=float,
        help='Detection confidence threshold for starting a new track')
    parser.add_argument(
        '--score-continue-min',
        default=0.7,
        type=float,
        help=('Detection confidence threshold for adding a detection to '
              'existing track.'))
    parser.add_argument(
        '--frames-skip-max',
        default=10,
        type=int,
        help='How many frames a track is allowed to miss detections in.')
    parser.add_argument(
        '--spatial-dist-max',
        default=0.2,
        type=float,
        help=('Maximum distance between matched detections, as a fraction of '
              'the image diagonal.'))
    parser.add_argument(
        '--area-ratio',
        default=0.5,
        type=float,
        help=('Specifies threshold for area ratio between matched detections. '
              'To match two detections, the ratio between their areas must '
              'be between this threshold and 1/threshold. Setting this to '
              '0 disables checking of area ratios.'))
    parser.add_argument(
        '--iou-min',
        default=0.1,
        type=float,
        help='Minimum IoU between matched detections.')
    parser.add_argument(
        '--iou-gap-min',
        default=0,
        type=float,
        help=('Minimum gap between the best IoU and the second best IoU of '
              'detections to a track. For each track, if the detection with '
              'highest IoU has IoU > IoU of second highest IoU detection + '
              'gap, then the highest IoU detection is automatically assigned '
              'to the track.'))
    parser.add_argument(
        '--ignore-labels',
        action='store_true',
        help=('Ignore labels when matching detections. By default, we only '
              'match detections if their labels match.'))
    # TODO(achald): Add help text.
    parser.add_argument(
        '--appearance-feature',
        choices=['mask', 'histogram'],
        default='histogram')
    # TODO(achald): Add help text.
    parser.add_argument('--appearance-gap', default=0, type=float)

    parser = root_parser.add_argument_group('debug')
    parser.add_argument(
        '--draw-spatial-threshold',
        action='store_true',
        help='Draw a diagnostic showing the spatial distance threshold')


def load_detectron_pickles(detectron_input, get_framenumber):
    """Load detectron pickle files from a directory.

    Returns:
        dict mapping pickle filename to data in pickle file.
    """
    detection_results = {}
    for x in detectron_input.glob('*.pickle'):
        if x.stem == 'merged':
            logging.warn('Ignoring merged.pickle for backward compatibility')
            continue

        try:
            get_framenumber(x.stem)
        except ValueError:
            logging.fatal('Expected pickle files to be named <frame_id>.pickle'
                          ', found %s.' % x)
            raise

        with open(x, 'rb') as f:
            detection_results[x.stem] = pickle.load(f)
    return detection_results


def main():
    tracking_parser = create_tracking_parser()

    # Use first line of file docstring as description if it exists.
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n')[0] if __doc__ else '',
        parents=[tracking_parser],
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--detectron-dir', type=Path, required=True)
    parser.add_argument('--images-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--output-video', type=Path)
    parser.add_argument('--output-video-fps', default=10, type=float)
    parser.add_argument(
        '--output-track-file',
        type=Path,
        help='Optional; path to output MOT17 style tracking output.')
    parser.add_argument('--extension', default='.png')
    parser.add_argument(
        '--dataset', default='coco', choices=['coco', 'objectness'])
    parser.add_argument(
        '--filename-format', choices=['frame', 'sequence_frame', 'fbms'],
        default='frame',
        help=('Specifies how to get frame number from the filename. '
              '"frame": the filename is the frame number, '
              '"sequence_frame": frame number is separated by an underscore'
              '"fbms": assume fbms style frame numbers'))

    debug_parser = parser.add_argument_group('debug')
    debug_parser.add_argument(
        '--draw-spatial-threshold',
        action='store_true',
        help='Draw a diagnostic showing the spatial distance threshold')

    tracking_params, remaining_argv = tracking_parser.parse_known_args()
    args = parser.parse_args(remaining_argv)

    tracking_params = vars(tracking_params)

    assert (args.output_dir is not None
            or args.output_video is not None
            or args.output_track_file is not None), (
            'One of --output-dir, --output-video, or --output-track-file must '
            'be specified.')

    if args.output_track_file is not None:
        output_log_file = (
            os.path.splitext(args.output_track_file)[0] + '-tracker.log')
    elif args.output_video is not None:
        output_log_file = (
            os.path.splitext(args.output_video)[0] + '-tracker.log')
    elif args.output_dir is not None:
        output_log_file = os.path.join(args.output_dir, 'tracker.log')
    setup_logging(output_log_file)
    logging.info('Printing source code to logging file')
    with open(__file__, 'r') as f:
        logging.debug(f.read())

    logging.info('Args: %s', pprint.pformat(vars(args)))
    logging.info('Tracking params: %s', pprint.pformat(tracking_params))

    detectron_input = args.detectron_dir
    if not detectron_input.is_dir():
        raise ValueError(
            '--detectron-dir %s is not a directory!' % args.detectron_dir)

    if args.filename_format == 'fbms':
        from utils.fbms.utils import get_framenumber
    elif args.filename_format == 'sequence_frame':
        def get_framenumber(x):
            return int(x.split('_')[-1])
    elif args.filename_format == 'frame':
        get_framenumber = int
    else:
        raise ValueError(
            'Unknown --filename-format: %s' % args.filename_format)

    detection_results = load_detectron_pickles(args.detectron_input,
                                               get_framenumber)
    frames = sorted(detection_results.keys(), key=get_framenumber)

    should_visualize = (args.output_dir is not None
                        or args.output_video is not None)
    should_output_mot = args.output_track_file is not None
    if should_output_mot:
        logging.info('Outputing MOT style tracks to %s',
                     args.output_track_file)

    label_list = get_classes(args.dataset)

    frame_paths = [
        args.images_dir / (frame + args.extension) for frame in frames
    ]
    # To filter tracks to only focus on people, add
    #   filter_label=label_list.index('person'))
    all_tracks = track(frame_paths,
                       [detection_results[frame] for frame in frames],
                       tracking_params)

    if should_output_mot:
        logging.info('Outputting MOT style tracks')
        output_mot_tracks(all_tracks, label_list,
                          [get_framenumber(x[1]) for x in frames],
                          args.output_track_file)
        logging.info('Output tracks to %s' % args.output_track_file)

    if should_visualize:
        logging.info('Visualizing tracks')
        visualize_tracks(all_tracks, frame_paths, args.dataset,
                         tracking_params, args.output_dir, args.output_video,
                         args.output_video_fps)


if __name__ == "__main__":
    main()
