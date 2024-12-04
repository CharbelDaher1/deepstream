import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst, GLib
import pyds
import sys
import os
import time
from datetime import datetime
import traceback

videoconvert = None

def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("End-of-stream")
        loop.quit()
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print("Error: %s: %s\n" % (err, debug))
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        err, debug = message.parse_warning()
        print("Warning: %s: %s\n" % (err, debug))
    return True

def osd_sink_pad_buffer_probe(pad, info):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
        except StopIteration:
            break

        print(f"\nFrame Number={frame_meta.frame_num}")
        l_obj = frame_meta.obj_meta_list
        while l_obj is not None:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                print(f"\nObject ID: {obj_meta.object_id}")
                print(f"Label: {obj_meta.obj_label}")
                print(f"Class ID: {obj_meta.class_id}")
                print(f"Confidence: {obj_meta.confidence}")
                
                if obj_meta.classifier_meta_list:
                    print("\nClassifier Metadata found:")
                    cls_meta = obj_meta.classifier_meta_list
                    while cls_meta:
                        cls = pyds.NvDsClassifierMeta.cast(cls_meta.data)
                        print(f"Component ID: {cls.unique_component_id}")
                        
                        label_info = cls.label_info_list
                        while label_info:
                            label = pyds.glist_get_nvds_label_info(label_info.data)
                            print(f"Label: {label.result_label}")
                            print(f"Confidence: {label.result_prob}")
                            
                            try:
                                label_info = label_info.next
                            except StopIteration:
                                break
                            
                        try:
                            cls_meta = cls_meta.next
                        except StopIteration:
                            break
                else:
                    print("No classifier metadata found")
                
            except StopIteration:
                break

            try:
                l_obj = l_obj.next
            except StopIteration:
                break

        try:
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.OK

def decoder_pad_added(dbin, pad):
    print("Pad added:", pad.get_name())
    if pad.get_current_caps().get_structure(0).get_name().startswith("video/"):
        print("Linking decoder pad to videoconvert")
        sink_pad = videoconvert.get_static_pad("sink")
        if not sink_pad.is_linked():
            pad.link(sink_pad)

def main():
    global videoconvert
    
    Gst.init(None)

    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write(" Unable to create Pipeline \n")
        sys.exit(1)

    print("\nCreating Pipeline Elements...")

    source = Gst.ElementFactory.make("filesrc", "file-source")
    if not source:
        sys.stderr.write(" Unable to create source \n")
        sys.exit(1)
    source.set_property('location', "car.jpg")

    decoder = Gst.ElementFactory.make("decodebin", "image-decoder")
    if not decoder:
        sys.stderr.write(" Unable to create decoder \n")
        sys.exit(1)

    videoconvert = Gst.ElementFactory.make("videoconvert", "video-convert")
    if not videoconvert:
        sys.stderr.write(" Unable to create videoconvert \n")
        sys.exit(1)

    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    if not streammux:
        sys.stderr.write(" Unable to create streammux \n")
        sys.exit(1)

    pgie = Gst.ElementFactory.make("nvinfer", "primary-inference")
    if not pgie:
        sys.stderr.write(" Unable to create pgie \n")
        sys.exit(1)

    sgie = Gst.ElementFactory.make("nvinfer", "secondary-inference")
    if not sgie:
        sys.stderr.write(" Unable to create sgie \n")
        sys.exit(1)

    tgie = Gst.ElementFactory.make("nvinfer", "tertiary-inference")
    if not tgie:
        sys.stderr.write(" Unable to create tgie \n")
        sys.exit(1)

    nvvidconv = Gst.ElementFactory.make("nvvideoconvert", "convertor")
    if not nvvidconv:
        sys.stderr.write(" Unable to create nvvidconv \n")
        sys.exit(1)

    nvosd = Gst.ElementFactory.make("nvdsosd", "onscreendisplay")
    if not nvosd:
        sys.stderr.write(" Unable to create nvosd \n")
        sys.exit(1)

    nvvidconv2 = Gst.ElementFactory.make("nvvideoconvert", "convertor2")
    if not nvvidconv2:
        sys.stderr.write(" Unable to create nvvidconv2 \n")
        sys.exit(1)

    capsfilter = Gst.ElementFactory.make("capsfilter", "capsfilter")
    if not capsfilter:
        sys.stderr.write(" Unable to create capsfilter \n")
        sys.exit(1)

    jpegenc = Gst.ElementFactory.make("jpegenc", "jpegenc")
    if not jpegenc:
        sys.stderr.write(" Unable to create jpegenc \n")
        sys.exit(1)

    filesink = Gst.ElementFactory.make("filesink", "filesink")
    if not filesink:
        sys.stderr.write(" Unable to create filesink \n")
        sys.exit(1)

    print("All elements created successfully")

    streammux.set_property('width', 1920)
    streammux.set_property('height', 1080)
    streammux.set_property('batch-size', 1)
    streammux.set_property('batched-push-timeout', 4000000)
    streammux.set_property('live-source', 0)

    print("Setting config files...")
    pgie.set_property('config-file-path', 'spec_files/traffic_config.txt')
    sgie.set_property('config-file-path', 'spec_files/lpd_config.txt')
    tgie.set_property('config-file-path', 'spec_files/lpr_config.txt')

    caps = Gst.Caps.from_string("video/x-raw, format=I420")
    capsfilter.set_property("caps", caps)
    jpegenc.set_property('quality', 85)
    filesink.set_property('location', 'output_processed.jpg')
    filesink.set_property('sync', False)

    print("Adding elements to pipeline...")
    elements = [source, decoder, videoconvert, streammux, pgie, sgie, tgie,
               nvvidconv, nvosd, nvvidconv2, capsfilter, jpegenc, filesink]

    for element in elements:
        pipeline.add(element)

    print("Linking elements...")
    
    if not source.link(decoder):
        sys.stderr.write(" Unable to link source to decoder\n")
        sys.exit(1)

    decoder.connect("pad-added", decoder_pad_added)

    sinkpad = streammux.get_request_pad("sink_0")
    if not sinkpad:
        sys.stderr.write(" Unable to get the sink pad of streammux\n")
        sys.exit(1)

    srcpad = videoconvert.get_static_pad("src")
    if not srcpad:
        sys.stderr.write(" Unable to get source pad of videoconvert\n")
        sys.exit(1)
    
    if srcpad.link(sinkpad) != Gst.PadLinkReturn.OK:
        sys.stderr.write(" Failed to link videoconvert to streammux\n")
        sys.exit(1)

    elements_to_link = [streammux, pgie, sgie, tgie, nvvidconv, nvosd, 
                       nvvidconv2, capsfilter, jpegenc, filesink]

    for i in range(len(elements_to_link) - 1):
        if not elements_to_link[i].link(elements_to_link[i + 1]):
            sys.stderr.write(" Unable to link %s to %s\n" % (
                elements_to_link[i].get_name(), 
                elements_to_link[i + 1].get_name()))
            sys.exit(1)

    print("Adding probe...")
    osdsinkpad = nvosd.get_static_pad("sink")
    if not osdsinkpad:
        sys.stderr.write(" Unable to get sink pad of nvosd\n")
        sys.exit(1)
    osdsinkpad.add_probe(Gst.PadProbeType.BUFFER, osd_sink_pad_buffer_probe)

    print("Creating pipeline bus...")
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    print("Starting pipeline...")
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        sys.stderr.write(" Unable to set the pipeline to the playing state.\n")
        sys.exit(1)

    try:
        print("Running pipeline...")
        loop.run()
    except:
        print("Error occurred. Getting last error...")
        bus.timed_pop_filtered(Gst.CLOCK_TIME_NONE, Gst.MessageType.ERROR)
    finally:
        print("Cleaning up...")
        pipeline.send_event(Gst.Event.new_eos())
        
        time.sleep(2)
        
        pipeline.set_state(Gst.State.NULL)
        
        if os.path.exists('output_processed.jpg'):
            size = os.path.getsize('output_processed.jpg')
            print(f"Output file size: {size} bytes")
            if size == 0:
                print("Warning: Output file is empty!")
        else:
            print("Error: Output file was not created!")

if __name__ == '__main__':
    sys.exit(main())
