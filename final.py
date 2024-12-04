import gi
gi.require_version('Gst', '1.0')
from gi.repository import GObject, Gst, GLib
import pyds
import sys
import time
import os
from pathlib import Path
import shutil

class LPRPipeline:
    def __init__(self):
        self.current_file = None
        self.current_image_path = None
        self.output_dir = Path("recognized_plates")
        self.output_dir.mkdir(exist_ok=True)
        Gst.init(None)
        
        # Initialize pipeline and elements once
        self.pipeline = Gst.Pipeline()
        self.source = Gst.ElementFactory.make("filesrc", "file-source")
        self.decoder = Gst.ElementFactory.make("decodebin", "image-decoder")
        self.videoconvert = Gst.ElementFactory.make("videoconvert", "video-convert")
        self.streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
        self.lprnet = Gst.ElementFactory.make("nvinfer", "lpr-inference")
        self.fakesink = Gst.ElementFactory.make("fakesink", "fakesink")

        # Configure static properties
        self.streammux.set_property('width', 720)
        self.streammux.set_property('height', 320)
        self.streammux.set_property('batch-size', 1)
        self.streammux.set_property('batched-push-timeout', 4000000)
        self.streammux.set_property('live-source', 0)
        self.lprnet.set_property('config-file-path', 'spec_files/lpr_config.txt')

        # Add elements to pipeline
        elements = [self.source, self.decoder, self.videoconvert, 
                   self.streammux, self.lprnet, self.fakesink]
        for element in elements:
            if not element:
                raise RuntimeError("Failed to create elements")
            self.pipeline.add(element)

        # Link static elements
        self.decoder.connect("pad-added", self.decoder_pad_added, self.videoconvert)
        sinkpad = self.streammux.get_request_pad("sink_0")
        srcpad = self.videoconvert.get_static_pad("src")
        if not srcpad.link(sinkpad) == Gst.PadLinkReturn.OK:
            raise RuntimeError("Failed to link videoconvert to streammux")
        
        if not self.streammux.link(self.lprnet):
            raise RuntimeError("Failed to link streammux to lprnet")
        if not self.lprnet.link(self.fakesink):
            raise RuntimeError("Failed to link lprnet to fakesink")

        # Add probe
        infer_pad = self.lprnet.get_static_pad("src")
        infer_pad.add_probe(Gst.PadProbeType.BUFFER, self.inference_pad_buffer_probe)

    def bus_call(self, bus, message, loop):
        t = message.type
        if t == Gst.MessageType.EOS:
            print(f"Finished processing: {self.current_file}")
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            warn, debug = message.parse_warning()
            print(f"Warning: {warn}: {debug}\n")
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print(f"Error: {err}: {debug}\n")
            loop.quit()
        return True

    def save_image_with_plate_number(self, plate_number, confidence):
        if confidence < 0.5:
            return
        plate_number = plate_number.strip().replace(' ', '_')
        file_extension = Path(self.current_file).suffix
        new_filename = f"{plate_number}{file_extension}"
        output_path = self.output_dir / new_filename
        
        counter = 1
        while output_path.exists():
            new_filename = f"{plate_number}_{counter}{file_extension}"
            output_path = self.output_dir / new_filename
            counter += 1
            
        try:
            shutil.copy2(self.current_image_path, output_path)
            print(f"Saved image as: {new_filename} (confidence: {confidence:.2f})")
        except Exception as e:
            print(f"Error saving image: {str(e)}")

    def inference_pad_buffer_probe(self, pad, info):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            print("Unable to get GstBuffer ")
            return

        try:
            batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(gst_buffer))
            l_frame = batch_meta.frame_meta_list
            while l_frame is not None:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
                l_obj = frame_meta.obj_meta_list
                while l_obj is not None:
                    obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                    if obj_meta.classifier_meta_list:
                        cls_meta = obj_meta.classifier_meta_list
                        while cls_meta:
                            cls = pyds.NvDsClassifierMeta.cast(cls_meta.data)
                            label_info = cls.label_info_list
                            while label_info:
                                label = pyds.glist_get_nvds_label_info(label_info.data)
                                self.save_image_with_plate_number(label.result_label, label.result_prob)
                                label_info = label_info.next
                            cls_meta = cls_meta.next
                    l_obj = l_obj.next
                l_frame = l_frame.next
        except Exception as e:
            print(f"Error in buffer probe: {str(e)}")
        return Gst.PadProbeReturn.DROP

    def decoder_pad_added(self, dbin, pad, videoconvert):
        if pad.get_current_caps().get_structure(0).get_name().startswith("video/"):
            sink_pad = videoconvert.get_static_pad("sink")
            if not sink_pad.is_linked():
                pad.link(sink_pad)

    def process_image(self, image_path):
        self.current_image_path = image_path
        self.current_file = os.path.basename(str(image_path))
        print(f"Processing: {self.current_file}")

        # Update source location
        self.source.set_property('location', str(image_path))
        self.source.link(self.decoder)

        # Create a new main loop for this image
        loop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.bus_call, loop)

        # Set to playing state
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print(f"Failed to set pipeline to PLAYING state for {image_path}")
            return False

        try:
            # Increase timeout to 30 seconds
            GLib.timeout_add_seconds(30, lambda: loop.quit())
            loop.run()
        except Exception as e:
            print(f"Error in processing loop: {str(e)}")
            return False
        finally:
            # Reset pipeline state between images
            self.pipeline.set_state(Gst.State.NULL)
            # Wait for state change to complete
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        return True

def main():
    lpr_pipeline = LPRPipeline()
    try:
        image_folder = Path("plate_images_processed")
        image_files = list(image_folder.glob("*.jpg")) + \
                     list(image_folder.glob("*.jpeg")) + \
                     list(image_folder.glob("*.png"))
        
        for image_file in image_files:
            if not lpr_pipeline.process_image(image_file):
                print(f"Failed to process {image_file}, continuing with next image")
            time.sleep(1)  # Add small delay between images
            
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == '__main__':
    main()

#issue is that it breaks when there is a certain faulty image
