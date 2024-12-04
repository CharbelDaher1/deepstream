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
        # Create output directory if it doesn't exist
        self.output_dir.mkdir(exist_ok=True)
        Gst.init(None)

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
        if confidence < 0.5:  # You can adjust this threshold
            return
            
        # Clean the plate number to make it filesystem-friendly
        plate_number = plate_number.strip().replace(' ', '_')
        
        # Get the file extension from the original file
        file_extension = Path(self.current_file).suffix
        
        # Create the new filename with the plate number
        new_filename = f"{plate_number}{file_extension}"
        output_path = self.output_dir / new_filename
        
        # If file already exists, add a counter
        counter = 1
        while output_path.exists():
            new_filename = f"{plate_number}_{counter}{file_extension}"
            output_path = self.output_dir / new_filename
            counter += 1
            
        # Copy the original image with the new name
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
                                # Save the image immediately after detecting a plate
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

        # Create a new pipeline for each image
        pipeline = Gst.Pipeline()
        
        # Create elements
        source = Gst.ElementFactory.make("filesrc", "file-source")
        decoder = Gst.ElementFactory.make("decodebin", "image-decoder")
        videoconvert = Gst.ElementFactory.make("videoconvert", "video-convert")
        streammux = Gst.ElementFactory.make("nvstreammux", "stream-muxer")
        lprnet = Gst.ElementFactory.make("nvinfer", "lpr-inference")
        fakesink = Gst.ElementFactory.make("fakesink", "fakesink")

        if not all([source, decoder, videoconvert, streammux, lprnet, fakesink]):
            print("Failed to create elements")
            return False

        # Set properties
        source.set_property('location', str(image_path))
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
        decoder.connect("pad-added", self.decoder_pad_added, videoconvert)

        sinkpad = streammux.get_request_pad("sink_0")
        srcpad = videoconvert.get_static_pad("src")
        if not srcpad.link(sinkpad) == Gst.PadLinkReturn.OK:
            print("Failed to link videoconvert to streammux")
            return False

        if not streammux.link(lprnet):
            print("Failed to link streammux to lprnet")
            return False
        if not lprnet.link(fakesink):
            print("Failed to link lprnet to fakesink")
            return False

        # Add probe
        infer_pad = lprnet.get_static_pad("src")
        infer_pad.add_probe(Gst.PadProbeType.BUFFER, self.inference_pad_buffer_probe)

        # Create a new main loop
        loop = GLib.MainLoop()
        
        # Setup bus
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.bus_call, loop)

        # Start playing
        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            print(f"Failed to set pipeline to PLAYING state for {image_path}")
            return False

        try:
            # Run for maximum 10 seconds
            GLib.timeout_add_seconds(10, lambda: loop.quit())
            loop.run()
        except Exception as e:
            print(f"Error in processing loop: {str(e)}")
            return False
        finally:
            # Cleanup
            pipeline.set_state(Gst.State.NULL)
            
        return True

def main():
    lpr_pipeline = LPRPipeline()
    
    try:
        # Process all images in the folder
        image_folder = Path("plate_images")
        image_files = list(image_folder.glob("*.jpg")) + list(image_folder.glob("*.jpeg")) + list(image_folder.glob("*.png"))

        for image_file in image_files:
            if not lpr_pipeline.process_image(image_file):
                print(f"Failed to process {image_file}, continuing with next image")
            # Add a small delay between images
            time.sleep(2)
            
    except Exception as e:
        print(f"An error occurred: {str(e)}")

if __name__ == '__main__':
    main()