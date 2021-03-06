"""Evaluate predictions in numpy format with COCO-style evaluation.

This was used to evaluate the output of Margaret's method, after converting
the ppm outputs to a "results.npy" file, using fbms/parse_dat_to_numpy.py
or fbms/parse_ppm_to_numpy.py."""

import argparse
import gc
import json
import logging
import pickle
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np
import pycocotools.mask as mask_util
from tqdm import tqdm
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval

from utils.fbms.utils import get_framenumber, get_frameoffset
from utils.log import setup_logging


def load_numpy_annotations(input_dir,
                           groundtruth,
                           parse_frame_fn,
                           predictions_in_results_npy):
    image_to_predictions_numpy = {}
    for image in tqdm(groundtruth.imgs.values()):
        # Format example: TestSet/marple7/marple7_400.jpg
        path = Path(image['file_name'])
        sequence = path.parent.stem
        frame = parse_frame_fn(path.stem)

        if predictions_in_results_npy:
            annotation_path = input_dir / sequence / 'results.npy'
        else:
            annotation_path = input_dir / (sequence + '.npy')
        if not annotation_path.exists():
            raise ValueError('Annotation for sequence %s does not exist at %s'
                             % (sequence, annotation_path))
        video_annotation = np.load(annotation_path)
        if video_annotation.shape[0] < frame:
            raise ValueError(
                'Could not find annotation for sequence %s, frame %s at %s. '
                'Only found %s frames' %
                (sequence, frame, annotation_path, video_annotation.shape[0]))
        # If we don't call copy, the original full video_annotation stays in
        # memory.
        annotation = video_annotation[frame].copy()
        expected_shape = (image['height'], image['width'])
        assert annotation.shape[:2] == expected_shape, (
            'Unexpected annotation shape for sequence {seq}, frame {frame}.\n'
            'Expected: ({ew}x{eh}), saw: ({sw}x{sh}).'.format(
                seq=sequence, frame=frame,
                ew=image['width'], eh=image['height'],
                sw=annotation.shape[1], sh=annotation.shape[0]))
        image_to_predictions_numpy[image['id']] = annotation
    return image_to_predictions_numpy


def main():
    # Use first line of file docstring as description if it exists.
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n')[0] if __doc__ else '',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--input-dir',
        help=('Contains numpy file of predictions for each sequence. Each '
              'file should be named {sequence}.npy and contain a (height, '
              'width, num_frames) numpy array. Also see '
              '--predictions-in-subdir for alternative directory structure.'))
    parser.add_argument(
        '--predictions-pickle',
        help=('Use pickle file containing dictionary mapping image_id to '
              'prediction numpy array, instead of --input-dir. This can also '
              'be used to resume from a previous run of this script, as this '
              'script dumps an "predictions.pkl" file in the output dir. '
              'Mutually exclusive with --input-dir.'))
    parser.add_argument(
        '--filename-format',
        choices=['frame', 'sequence_frame', 'sequence-frame', 'fbms'],
        default='frame',
        help=('Specifies how to get frame number from the "file_name" stored '
              'in COCO annotations. '
              '"frame": the filename is the frame number, '
              '"sequence_frame": frame number is separated by an underscore, '
              '"sequence-frame": frame number is separated by a dash, '
              '"fbms": assume fbms style frame numbers'))
    parser.add_argument('--annotations-json', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument(
        '--visualize', action='store_true', help='Whether to visualize masks.')
    parser.add_argument(
        '--no-remove-largest-mask',
        dest='remove_largest_mask',
        action='store_false',
        help=('By default, we remove the mask with the largest area, making '
              'the assumption that it is background. Setting this flag '
              'avoids this behavior (i.e. keeps the largest area mask).'))
    parser.add_argument(
        '--min-mask-portion',
        default=0.001,
        type=float,
        help='Remove masks that are less than this portion of image size.')
    parser.add_argument(
        '--predictions-in-subdir',
        action='store_true',
        help=('If true, assume --input-dir contains a subdirectory for each '
              'sequence, each of which contains a "results.npy" file.'))

    args = parser.parse_args()

    assert (args.input_dir is None) != (args.predictions_pickle is None), (
        'Exactly one of --input-dir or --predictions-pickle required.')

    if args.input_dir:
        input_dir = Path(args.input_dir)
        assert input_dir.exists()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(exist_ok=True, parents=True)

    _source_path = Path(__file__)
    logging_path = output_dir / (
        _source_path.stem + _source_path.suffix + '.log')
    setup_logging(str(logging_path))

    logging.info('Args:\n%s', vars(args))

    groundtruth = COCO(args.annotations_json)

    if args.filename_format == 'fbms':
        def parse_frame_offset(path):
            return get_frameoffset(path.parent.stem, get_framenumber(path))
    elif args.filename_format == 'sequence-frame':
        def parse_frame_offset(x):
            return int(x.split('-')[-1])
    elif args.filename_format == 'sequence_frame':
        def parse_frame_offset(x):
            return int(x.split('_')[-1])
    elif args.filename_format == 'frame':
        parse_frame_offset = int
    else:
        raise ValueError(
            'Unknown --filename-format: %s' % args.filename_format)

    if args.input_dir:
        image_to_predictions_numpy = load_numpy_annotations(
            input_dir,
            groundtruth,
            parse_frame_offset,
            predictions_in_results_npy=args.predictions_in_subdir)
        with open(output_dir / 'predictions.pkl', 'wb') as f:
            pickle.dump(image_to_predictions_numpy, f)
    else:
        # Use to continue from a previous run.
        with open(args.predictions_pickle, 'rb') as f:
            image_to_predictions_numpy = pickle.load(f)

    if args.visualize:
        from PIL import Image
        max_masks_per_image = max(
            len(np.unique(x)) for x in image_to_predictions_numpy.values())
        colors = (np.random.rand(max_masks_per_image, 3) * 256).round()
        vis_dir = output_dir / 'vis'
        vis_dir.mkdir()

    annotations = []
    for image_id, annotation_np in tqdm(image_to_predictions_numpy.items()):
        ids = sorted(np.unique(annotation_np))
        masks = [annotation_np == object_id for object_id in ids]
        # Sort masks by area
        masks = sorted(masks, key=lambda mask: mask.sum())
        if args.remove_largest_mask:
            masks = masks[:-1]  # Remove mask with largest area (background)

        if args.visualize:
            vis_mask = np.zeros(
                (annotation_np.shape[0], annotation_np.shape[1], 3),
                dtype=np.uint8)
            for i, mask in enumerate(masks):
                vis_mask[mask] = colors[i]
            Image.fromarray(vis_mask).save(vis_dir / ('%s.png' % image_id))

        if not masks:
            continue
        image_area = masks[0].shape[0] * masks[0].shape[1]
        masks_np = np.array(masks, dtype=np.uint8).transpose(1, 2, 0)
        rle_masks = mask_util.encode(np.asfortranarray(masks_np))
        for rle_mask in rle_masks:
            # See https://github.com/cocodataset/cocoapi/issues/70
            rle_mask['counts'] = rle_mask['counts'].decode('ascii')
            area = mask_util.area(rle_mask).item()
            ratio = area / image_area
            score = 0.75  # ratio * 0.3 + 0.7  # Map to (0.7, 1) range
            if ratio < args.min_mask_portion:
                continue
            annotations.append({
                'image_id': image_id,
                'segmentation': rle_mask,
                'category_id': 1,
                'score': score,
                'area': ratio,
                'bbox': mask_util.toBbox(rle_mask).tolist()
            })

    for ann_id, ann in enumerate(annotations):
        ann['id'] = ann_id + 1
        ann['iscrowd'] = 0

    with open(output_dir / 'results.json', 'w') as f:
        json.dump(annotations, f)

    predictions = groundtruth.loadRes(annotations)

    coco_eval = COCOeval(groundtruth, predictions, 'bbox')
    coco_eval.evaluate()
    coco_eval.accumulate()
    summary_f = StringIO()
    with redirect_stdout(summary_f):
        coco_eval.summarize()
    logging.info('Detection evaluation summary:\n%s', summary_f.getvalue())

    coco_eval = COCOeval(groundtruth, predictions, 'segm')
    coco_eval.evaluate()
    coco_eval.accumulate()
    summary_f = StringIO()
    with redirect_stdout(summary_f):
        coco_eval.summarize()
    logging.info('Segmentation evaluation summary:\n%s', summary_f.getvalue())


if __name__ == "__main__":
    main()
