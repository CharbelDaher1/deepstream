import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst, GLib
import pyds
import sys
import time

videoconvert = None
loop = None

def bus_call(bus, message, loop):
    t = message.type
    if t == Gst.MessageType.EOS:
        print("End-of-stream")
        loop.quit()
    elif t == Gst.MessageType.WARNING:
        warn, debug = message.parse_warning()
        print("Warning: %s: %s\n" % (warn, debug))
    elif t == Gst.MessageType.ERROR:
        err, debug = message.parse_error()
        print("Error: %s: %s\n" % (err, debug))
        loop.quit()
    return True

def inference_pad_buffer_probe(pad, info):
    gst_buffer = info.get_buffer()
    if not gst_buffer:
        print("Unable to get GstBuffer ")
        return

    batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
    l_frame = batch_meta.frame_meta_list
    while l_frame is not None:
        try:
            frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    if obj_meta.classifier_meta_list:
                        cls_meta = obj_meta.classifier_meta_list
                        while cls_meta:
                            cls = pyds.NvDsClassifierMeta.cast(cls_meta.data)
                            label_info = cls.label_info_list
                            while label_info:
                                label = pyds.glist_get_nvds_label_info(label_info.data)
                                print(f"License Plate Text: {label.result_label}")
                                print(f"Confidence: {label.result_prob}")
                                label_info = label_info.next
                            cls_meta = cls_meta.next
                    l_obj = l_obj.next
                except StopIteration:
                    break
            l_frame = l_frame.next
        except StopIteration:
            break

    return Gst.PadProbeReturn.DROP  # Drop the buffer since we don't need to process it further

def decoder_pad_added(dbin, pad):
    if pad.get_current_caps().get_structure(0).get_name().startswith("video/"):
        sink_pad = videoconvert.get_static_pad("sink")
        if not sink_pad.is_linked():
            pad.link(sink_pad)

def main():
    global videoconvert, loop
    
    Gst.init(None)

    pipeline = Gst.Pipeline()
    if not pipeline:
        raise RuntimeError("Unable to create Pipeline")

    # Create minimal elements needed
    source = Gst.ElementFactory.make("filesrc", "file-source")
    decoder = Gst.ElementFactory.make("decodebin", "image-decoder")
    videoconvert = Gst.ElementFactory.make("videoconvert", "video-convert")
    streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
    lprnet = Gst.ElementFactory.make("nvinfer", "lpr-inference")
    fakesink = Gst.ElementFactory.make("fakesink", "fakesink")

    if not all([source, decoder, videoconvert, streammux, lprnet, fakesink]):
        raise RuntimeError("Failed to create elements")

    # Set properties
    source.set_property('location', "2785ASR.jpg")
    streammux.set_property('width', 720)
    streammux.set_property('height', 320)
    streammux.set_property('batch-size', 1)
    streammux.set_property('batched-push-timeout', 4000000)
    streammux.set_property('live-source', 0)
    
    lprnet.set_property('config-file-path', 'spec_files/lpr_config.txt')

    # Add elements to pipeline
    for element in [source, decoder, videoconvert, streammux, lprnet, fakesink]:
        pipeline.add(element)

    # Link elements
    source.link(decoder)
    decoder.connect("pad-added", decoder_pad_added)

    sinkpad = streammux.get_request_pad("sink_0")
    srcpad = videoconvert.get_static_pad("src")
    if not srcpad.link(sinkpad) == Gst.PadLinkReturn.OK:
        raise RuntimeError("Failed to link videoconvert to streammux")

    # Link remaining elements
    if not streammux.link(lprnet):
        raise RuntimeError("Failed to link streammux to lprnet")
    if not lprnet.link(fakesink):
        raise RuntimeError("Failed to link lprnet to fakesink")

    # Add probe right after inference
    infer_pad = lprnet.get_static_pad("src")
    infer_pad.add_probe(Gst.PadProbeType.BUFFER, inference_pad_buffer_probe)

    # Create and run loop
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    # Start playing
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        raise RuntimeError("Unable to set the pipeline to the playing state")

    try:
        loop.run()
    except:
        pass
    finally:
        pipeline.send_event(Gst.Event.new_eos())
        time.sleep(2)
        pipeline.set_state(Gst.State.NULL)

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"An error occurred: {str(e)}")