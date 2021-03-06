#!/usr/bin/env python3

# SPDX-License-Identifier: MIT

import argparse
import copy
import os
import subprocess
from urllib.parse import parse_qs, urlparse
from collections import namedtuple
from queue import Queue
from threading import Thread
from xml.etree import ElementTree

import requests

namespaces = {"svg": "http://www.w3.org/2000/svg",
              "xlink": "http://www.w3.org/1999/xlink"}

Image = namedtuple("Image", ["id", "fname", "ts_in", "ts_out"])
Frame = namedtuple("Frame", ["fname", "ts_in", "ts_out"])


class Scrape:
    def __init__(self, host, id):
        self.host = host
        self.baseurl = "https://{}/presentation/{}".format(host, id)
        self.id = id

    def create_output_dir(self):
        self.out = "bbb-scrape-{}".format(self.id)
        try:
            os.mkdir(self.out)
        except FileExistsError:
            pass

    def fetch_shapes(self):
        url = "{}/shapes.svg".format(self.baseurl)
        shapes = requests.get(url)
        self.shapes = ElementTree.fromstring(shapes.content)
        open(os.path.join(self.out, "shapes.svg"), "wb").write(shapes.content)

    def fetch_deskshare(self):
        url = "{}/deskshare/deskshare.mp4".format(self.baseurl)
        req = requests.get(url)
        if req.status_code == 200:
            fname = os.path.join(self.out, "deskshare.mp4")
            open(fname, "wb").write(req.content)
            return True
        return False

    def fetch_webcams(self):
        url = "{}/video/webcams.mp4".format(self.baseurl)
        req = requests.get(url)
        if req.status_code == 200:
            fname = os.path.join(self.out, "webcams.mp4")
            open(fname, "wb").write(req.content)
            return True
        return False

    def fetch_image(self):
        while True:
            e = self.workq.get()
            href = e.attrib["{http://www.w3.org/1999/xlink}href"]
            fname = os.path.basename(href)
            url = "{}/{}".format(self.baseurl, href)
            image = requests.get(url)
            open(os.path.join(self.out, fname), "wb").write(image.content)
            e.attrib["{http://www.w3.org/1999/xlink}href"] = fname
            if "id" in e.attrib:
                self.images.append(Image(id=e.attrib["id"],
                                         fname=fname,
                                         ts_in=float(e.attrib["in"]),
                                         ts_out=float(e.attrib["out"])))
            self.workq.task_done()

    def fetch_images(self, tree=None):
        if tree is None:
            self.images = []
            self.workq = Queue()
            for i in range(os.cpu_count()):
                Thread(target=Scrape.fetch_image, args=(self,)).start()
            self.fetch_images(self.shapes)
            fname = os.path.join(self.out, "shapes.svg")
            open(fname, "wb").write(ElementTree.tostring(self.shapes))
            self.workq.join()
            return

        for e in tree.findall("svg:image", namespaces):
            self.workq.put(e)
        for e in tree:
            self.fetch_images(e)

    def read_timestamps(self, tree=None):
        if tree is None:
            self.timestamps = []
            self.read_timestamps(self.shapes)
            self.timestamps = list(dict.fromkeys(self.timestamps))
            self.timestamps.sort()
            return

        for e in tree:
            if "in" in e.attrib:
                self.timestamps.append(float(e.attrib["in"]))
            if "out" in e.attrib:
                self.timestamps.append(float(e.attrib["out"]))
            if "timestamp" in e.attrib:
                self.timestamps.append(float(e.attrib["timestamp"]))
            self.read_timestamps(e)

    def generate_frames(self):
        try:
            os.mkdir(os.path.join(self.out, "frames"))
        except FileExistsError:
            pass
        self.frames = []

        self.workq = Queue()
        for i in range(os.cpu_count()):
            Thread(target=Scrape.generate_frame, args=(self,)).start()

        t = 0.0
        for ts in self.timestamps[1:]:
            self.workq.put((t, ts))
            t = ts
        self.workq.join()

    def generate_frame(self):
        while True:
            (timestamp, ts_out) = self.workq.get()
            shapes = copy.deepcopy(self.shapes)
            image = None
            for i in self.images:
                if timestamp >= i.ts_in and timestamp < i.ts_out:
                    image = i.id
            for e in shapes.findall("svg:image", namespaces):
                if e.attrib["id"] == image:
                    e.attrib["style"] = ""
                else:
                    shapes.remove(e)
            for e in shapes.findall("svg:g", namespaces):
                assert(e.attrib["class"] == "canvas")
                if e.attrib["image"] == image:
                    e.attrib["display"] = "inherit"
                    self.make_visible(e, timestamp)
                else:
                    shapes.remove(e)
            fname = os.path.join("frames", "shapes{}.png".format(timestamp))
            fnamesvg = os.path.join("frames", "shapes{}.svg".format(timestamp))
            shapestr = ElementTree.tostring(shapes)
            open(os.path.join(self.out, fnamesvg), "wb").write(shapestr)
            subprocess.run(["inkscape", "--export-png={}".format(fname),
                            "--export-area-drawing", fnamesvg],
                           cwd=self.out, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
            frame = Frame(fname=fname, ts_in=timestamp, ts_out=ts_out)
            self.frames.append(frame)
            self.workq.task_done()

    def make_visible(self, tree, timestamp):
        for e in tree.findall("svg:g", namespaces):
            if ("timestamp" in e.attrib and
               float(e.attrib["timestamp"]) <= timestamp):
                style = e.attrib["style"].split(";")
                style.remove("visibility:hidden")
                e.attrib["style"] = ";".join(style)
            else:
                tree.remove(e)

    def generate_concat(self):
        f = open(os.path.join(self.out, "concat.txt"), "w")
        for frame in self.frames:
            f.write("file '{}'\n".format(frame.fname))
            f.write("duration {:f}\n".format(frame.ts_out-frame.ts_in))
        f.write("file '{}'\n".format(self.frames[-1].fname))
        f.close()

    def render_slides(self):
        subprocess.run(["ffmpeg", "-f", "concat", "-i", "concat.txt",
                        "-pix_fmt", "yuv420p", "-y", "slides.mp4"],
                       cwd=self.out, stderr=subprocess.PIPE)


def main():
    parser = argparse.ArgumentParser(description='Scrape Big Blue Button')
    parser.add_argument('host', help="Hostname or full Meeting URL")
    parser.add_argument('id', help="Meeting id", nargs='?')
    parser.add_argument('--no-webcam', action='store_true',
                        help="Don't scrape webcam")
    parser.add_argument('--no-deskshare', action='store_true',
                        help="Don't scrape deskshare")

    args = parser.parse_args()
    host = args.host
    meeting_id = args.id

    # If only one parameter is given, assume it is a full meeting URL and
    # extract host and meeting_id
    if meeting_id is None:
        url = urlparse(host)
        host = url.netloc
        if host is None:
            print("!! Bad meeting URL. Either specify hostname and meeting id"
                  " or the full URL of the recording")
            return 1
        qs = parse_qs(url.query)
        if "meetingId" not in qs:
            print("!! No meeting id given, and no meeting id found in URL")
            return 1
        meeting_id = qs["meetingId"][0]
    else:
        print("ii Usage of <host> <id> arguments is deprecated. "
              "Use <meeting url> as single argument instead.")

    try:
        subprocess.run(["ffmpeg", "-h"],
                       stderr=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL)
    except OSError:
        print("!! ffmpeg not found. Please install it and make sure it is"
              " available in your PATH.")
        return 1

    try:
        subprocess.run(["inkscape", "--help"],
                       stderr=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL)
    except OSError:
        print("!! inkscape not found. Please install it and make sure it is"
              "available in your PATH.")
        return 1

    scrape = Scrape(host, meeting_id)
    print("++ Scrape from server")
    scrape.create_output_dir()
    scrape.fetch_shapes()
    scrape.fetch_images()
    if not args.no_webcam and scrape.fetch_webcams():
        print("++ Stored webcams to webcams.mp4")
    if not args.no_deskshare and scrape.fetch_deskshare():
        print("++ Stored desk sharing to deskshare.mp4")
    print("++ Generate frames")
    scrape.read_timestamps()
    scrape.generate_frames()
    scrape.generate_concat()
    print("++ Render slides.mp4")
    scrape.render_slides()
