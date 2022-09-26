import subprocess
import linecache
import math
import queue
import random
import sys
import threading

import torch


class KTData:
    def __init__(
        self, data_path: str, inputs=None, batch_size=None, seq_len=200, shuffle=False
    ):
        if inputs is None:
            inputs = ["q", "s"]
        self.inputs = inputs
        self.data = Lines(data_path, group=len(inputs) + 1)
        self.batch_size = batch_size
        self.seq_len = seq_len
        self.shuffle = shuffle

    def __iter__(self):
        return Iterator(
            Transform(self.data, self.inputs, self.seq_len),
            batch_size=self.batch_size,
            full_shuffle=self.shuffle,
            transform=lambda x: _transform_batch(x, self.inputs, self.seq_len),
            prefetch=True,
        )


def _transform_batch(batch, fields, seq_len):
    batch = list(zip(*batch))
    batch = [
        torch.nn.utils.rnn.pad_sequence(
            [torch.tensor(x) if isinstance(x, list) else x for x in item],
            batch_first=True,
            padding_value=-1,
        )
        for item in batch
    ]
    return Batch(batch, fields, seq_len)


class Batch:
    def __init__(self, data, fields, seq_len):
        self.data = data
        self.stoi = {f: i for i, f in enumerate(fields)}
        self.seq_len = seq_len

    def get(self, *fields):
        L = self.data[0].size(1)
        return [
            [
                self.data[self.stoi[f]][:, i * self.seq_len : (i + 1) * self.seq_len]
                for i in range(L // self.seq_len)
            ]
            for f in fields
        ]


class Transform:
    def __init__(self, data, inputs, seq_len):
        self.data = data
        self.inputs = inputs
        self.seq_len = seq_len

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        batch = self.data[index]

        # single sequence
        if isinstance(index, int):
            del batch[0]  # remove count
            items = [[int(x) for x in line.strip().split(",")] for line in batch]
            return [torch.tensor(item) for item in items]

        # batch
        items = []
        for lines in batch:
            del lines[0]  # remove count
            items.append([[int(x) for x in line.strip().split(",")] for line in lines])
        return _transform_batch(items, self.inputs, self.seq_len)


class Lines:
    def __init__(self, filename, skip=0, group=1, preserve_newline=False):
        self.filename = filename
        with open(filename):
            pass
        if sys.platform == "win32":
            linecount = sum(1 for _ in open(filename))
        else:
            output = subprocess.check_output(("wc -l " + filename).split())
            linecount = int(output.split()[0])
        self.length = (linecount - skip) // group
        self.skip = skip
        self.group = group
        self.preserve_newline = preserve_newline

    def __len__(self):
        return self.length

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]

    def __getitem__(self, item):
        d = self.skip + 1
        if isinstance(item, int):
            if item < len(self):
                if self.group == 1:
                    line = linecache.getline(self.filename, item + d)
                    if not self.preserve_newline:
                        line = line.strip("\r\n")
                else:
                    line = [
                        linecache.getline(self.filename, d + item * self.group + k)
                        for k in range(self.group)
                    ]
                    if not self.preserve_newline:
                        line = [l.strip("\r\n") for l in line]
                return line

        elif isinstance(item, slice):
            low = 0 if item.start is None else item.start
            low = _clip(low, -len(self), len(self) - 1)
            if low < 0:
                low += len(self)
            high = len(self) if item.stop is None else item.stop
            high = _clip(high, -len(self), len(self))
            if high < 0:
                high += len(self)
            ls = []
            for i in range(low, high):
                if self.group == 1:
                    line = linecache.getline(self.filename, i + d)
                    if not self.preserve_newline:
                        line = line.strip("\r\n")
                else:
                    line = [
                        linecache.getline(self.filename, d + i * self.group + k)
                        for k in range(self.group)
                    ]
                    if not self.preserve_newline:
                        line = [l.strip("\r\n") for l in line]
                ls.append(line)

            return ls

        raise IndexError


class Iterator:
    """Iterator on data and labels, with states for save and restore."""

    def __init__(
        self,
        data,
        *label,
        prefetch=False,
        length=None,
        batch_size=None,
        transform=None,
        shuffle=False,
        full_shuffle=False,
    ):
        self.data = data
        self.label = label
        self.prefetch = prefetch
        self.batch_size = batch_size
        self.queue = queue.Queue(maxsize=8)
        self.length = length if length is not None else len(data)
        self.transform = transform

        assert all(
            self.length == len(lab) for lab in label
        ), "data and label must have same lengths"

        self.index = list(range(len(self)))
        self.full_index = None
        if shuffle:
            random.shuffle(self.index)
        if full_shuffle:
            self.full_index = list(range(self.length))
            random.shuffle(self.full_index)

        self.thread = None
        self.pos = 0

    def __len__(self):
        if self.batch_size is None:
            return self.length
        return math.ceil(self.length / self.batch_size)

    def __iter__(self):
        return self

    def __next__(self):
        if self.thread is None and self.prefetch:
            self.thread = threading.Thread(target=self.produce, daemon=True)
            self.thread.start()

        if self.pos >= len(self.index):
            raise StopIteration

        if not self.prefetch:
            self.produce(False)
        item = self.queue.get()
        if isinstance(item, Exception):
            raise item
        else:
            self.pos += 1
            return item

    def produce(self, daemon=True):
        if self.batch_size is None:
            # no batch, direct indexing
            try:
                for i in range(self.pos, self.length):
                    data = self.data[i]
                    label = [label[i] for label in self.label]
                    if label:
                        self.queue.put([data] + label)
                    else:
                        self.queue.put(data)

                    if not daemon:
                        return

            except Exception as e:
                if daemon:
                    self.queue.put(e)
                    return
                else:
                    raise

        if self.full_index:
            for i in range(self.pos, len(self)):
                try:
                    bs = self.batch_size
                    inds = self.full_index[i * bs : (i + 1) * bs]

                    data_batch = [self.data[i] for i in inds]
                    if self.transform is not None:
                        data_batch = self.transform(data_batch)

                    label_batch = [
                        [label[i] for i in inds]
                        if self.transform is None
                        else self.transform([label[i] for i in inds])
                        for label in self.label
                    ]

                    if label_batch:
                        self.queue.put([data_batch] + label_batch)
                    else:
                        self.queue.put(data_batch)

                    if not daemon:
                        return

                except Exception as e:
                    if daemon:
                        self.queue.put(e)
                        return
                    else:
                        raise

        else:
            for i in range(self.pos, len(self)):
                try:
                    index = self.index[i]

                    bs = self.batch_size

                    if callable(self.data):
                        data_batch = self.data(index * bs, (index + 1) * bs)
                    else:
                        data_batch = self.data[index * bs : (index + 1) * bs]

                    label_batch = [
                        label(index * bs, (index + 1) * bs)
                        if callable(label)
                        else label[index * bs : (index + 1) * bs]
                        for label in self.label
                    ]

                    if label_batch:
                        self.queue.put([data_batch] + label_batch)
                    else:
                        self.queue.put(data_batch)

                    if not daemon:
                        return

                except Exception as e:
                    if daemon:
                        self.queue.put(e)
                        return
                    else:
                        raise


def _clip(v, low, high):
    if v < low:
        v = low
    if v > high:
        v = high
    return v
