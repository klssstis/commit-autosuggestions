# Copyright 2020-present Tae Hwan Jung
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import enum
import random
import logging
import tempfile
import argparse
import numpy as np
from tqdm import *
import whatthepatch
from git import Repo
from functools import partial
from multiprocessing.pool import Pool
from transformers import AutoTokenizer

from matorage import *

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - PID: %(process)d -  %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)

class PATCH(enum.Enum):
	PLUS=1
	MINUS=2

def truncate(tuple, max_length, value=0):
    ls = []
    for t in tuple:
        if isinstance(t, int):
            t = [t]
        ls.extend(t)
    ls = ls[:max_length - 1]
    ls.insert(0, value)
    if len(ls) < max_length:
        ls.extend([0] * (max_length - len(ls)))
    assert len(ls) == max_length
    return ls

def encode_line(tokenizer, line, patch):
    line = re.sub(r'[\u0100-\uFFFF\U00010000-\U0010FFFF]+', '', line).strip()
    tokens = tokenizer.tokenize(line)
    tokens = tokenizer.convert_tokens_to_ids(tokens)
    return (
        tokens,
        [1] * len(tokens),
        len(tokens) * [patch.value]
    )

def diff_parse(diff, tokenizer):
    chunks = []
    for diff in whatthepatch.parse_patch(diff):
        if diff.header.old_path != diff.header.new_path:
            chunks.append(encode_line(tokenizer, diff.header.old_path, PATCH.MINUS))
            chunks.append(encode_line(tokenizer, diff.header.new_path, PATCH.PLUS))
        if not diff.changes:
            continue
        for change in diff.changes:
            if change.old == None and change.new != None:
                chunks.append(encode_line(tokenizer, change.line, PATCH.PLUS))
            elif change.old != None and change.new == None:
                chunks.append(encode_line(tokenizer, change.line, PATCH.MINUS))
    return chunks

def sha_parse(sha, tokenizer, max_length=1024):

    chunks = diff_parse(diff=repo.git.show(sha), tokenizer=tokenizer)
    if not chunks:
        return None

    input_ids, attention_masks, patch_ids = zip(*chunks)
    input_ids = truncate(input_ids, max_length, value=0)
    attention_masks = truncate(attention_masks, max_length, value=1)
    patch_ids = truncate(patch_ids, max_length, value=0)

    return (input_ids, attention_masks, patch_ids)

def message_parse(msg, tokenizer, max_length=56):
    msg = re.sub(r'(\(|)#([0-9])+(\)|)', '', msg)

    msg = re.sub(r'[\u0100-\uFFFF\U00010000-\U0010FFFF]+', '', msg).strip()
    msg = tokenizer.tokenize(msg)
    msg = tokenizer.convert_tokens_to_ids(msg)
    msg = truncate(msg, max_length, value=0)

    return msg

def jobs(sha_msgs, args, data_config, train=True):

    input_ids, attention_masks, patch_ids, targets = [], [], [], []
    data_saver = DataSaver(config=data_config)

    for sha_msg in sha_msgs:
        sha, msg = sha_msg

        source = sha_parse(
            sha,
            tokenizer=args.tokenizer,
            max_length=args.max_source_length
        )
        if not source:
            continue
        input_id, attention_mask, patch_id = source
        target = message_parse(
            msg,
            tokenizer=args.tokenizer,
            max_length=(args.max_target_length if train else args.val_max_target_length),
        )

        input_ids.append(input_id)
        attention_masks.append(attention_mask)
        patch_ids.append(patch_id)
        targets.append(target)

    data_saver({
        "input_ids": np.asarray(input_ids),
        "attention_masks": np.asarray(attention_masks),
        "patch_ids": np.asarray(patch_ids),
        "targets": np.asarray(targets),
    })
    data_saver.disconnect()

def start(chunked_sha_msgs, train=True):

    logger.info(f"Start %s pre-processing" % ("training" if train else "evaluation"))

    max_target_length = args.max_target_length if train else args.val_max_target_length

    data_config = DataConfig(
        endpoint=args.endpoint,
        access_key=os.environ['access_key'],
        secret_key=os.environ['secret_key'],
        region=args.region,
        dataset_name='commit-autosuggestions',
        additional={
            "mode" : ("training" if train else "evaluation"),
            "max_source_length": args.max_source_length,
            "max_target_length": max_target_length,
            "url" : args.url,
        },
        attributes=[
            ('input_ids', 'int32', (args.max_source_length,)),
            ('attention_masks', 'int32', (args.max_source_length,)),
            ('patch_ids', 'int32', (args.max_source_length,)),
            ('targets', 'int32', (max_target_length,))
        ]
    )

    func = partial(jobs, args=args, data_config=data_config, train=train)
    with Pool(processes=args.num_workers) as pool:
        with tqdm(total=len(chunked_sha_msgs)) as pbar:
            for i, _ in tqdm(enumerate(pool.imap_unordered(func, chunked_sha_msgs))):
                pbar.update()

def main(args):
    if 'access_key' not in os.environ or 'secret_key' not in os.environ:
        raise OSError("access_key or secret_key are not found.")

    sha_msgs = [(c.hexsha, c.summary) for c in repo.iter_commits()]
    random.shuffle(sha_msgs)
    chunked_sha_msgs = [
        sha_msgs[x:x + args.matorage_batch]
        for x in range(0, len(sha_msgs), args.matorage_batch)
    ]

    barrier = int(len(chunked_sha_msgs) * (1 - args.p_val))
    if args.do_train:
        start(chunked_sha_msgs[:barrier], train=True)
    if args.do_predict:
        start(chunked_sha_msgs[barrier:], train=False)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code to collect commits on github")
    parser.add_argument(
        "--url",
        type=str,
        required=True,
        help="github url"
    )
    parser.add_argument(
        "--endpoint",
        type=str,
        required=True,
        help='matorage endpoint, check document of matorage: https://matorage.readthedocs.io/en/stable/storage.html'
    )
    parser.add_argument(
        "--region",
        type=str,
        default=None,
        help='matorage s3 region, check document of matorage: https://matorage.readthedocs.io/en/stable/storage.html'
    )
    parser.add_argument(
        "--tokenizer_name",
        default='sshleifer/distilbart-xsum-6-6',
        type=str,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )
    parser.add_argument(
        "--matorage_batch",
        default=1024,
        type=int,
        help='The smallest batch size stored atomically in matorage.'
    )
    parser.add_argument(
        "--num_workers",
        default=4,
        type=int,
        help="number of process",
    )
    parser.add_argument(
        "--max_source_length",
        default=1024,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
             "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument(
        "--max_target_length",
        default=56,
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
             "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument(
        "--val_max_target_length",
        default=142,  # these defaults are optimized for CNNDM. For xsum, see README.md.
        type=int,
        help="The maximum total input sequence length after tokenization. Sequences longer "
             "than this will be truncated, sequences shorter will be padded.",
    )
    parser.add_argument("--p_val", type=float, default=0.25, help="percent of validation dataset")
    parser.add_argument("--do_train", action="store_true", default=False)
    parser.add_argument("--do_predict", action="store_true", default=False)
    args = parser.parse_args()

    args.local_path = args.url.split('/')[-1]
    logger.info(f"master branch of {args.url} will be downloaded to {args.local_path}")
    repo = (
        Repo(args.local_path)
        if os.path.exists(args.local_path)
        else Repo.clone_from(args.url, to_path=args.local_path, branch="master")
    )
    args.tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name)

    main(args)
