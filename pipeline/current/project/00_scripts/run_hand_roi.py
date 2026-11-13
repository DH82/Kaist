"""Run the unchanged delivered candidate with existing PC batch=4 memory settings."""
import importlib.util,sys
from pathlib import Path

def main():
 path=Path(__file__).resolve().parent/'roi_camera.py'
 spec=importlib.util.spec_from_file_location('delivered_roi',path)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 original=module.importlib.util.module_from_spec
 def load_with_batch(spec):
  mod=original(spec)
  if spec.name=='source_sam':
   loader=spec.loader;old=loader.exec_module
   def execute(target):
    old(target);build=target.build_sam3_predictor
    def configured(*args,**kwargs):
     predictor=build(*args,**kwargs)
     predictor.model.batched_grounding_batch_size=4
     predictor.model.postprocess_batch_size=4
     return predictor
    target.build_sam3_predictor=configured
    from sam_progress import install
    install(target)
   loader.exec_module=execute
  return mod
 module.importlib.util.module_from_spec=load_with_batch
 try:module.main()
 finally:module.importlib.util.module_from_spec=original
if __name__=='__main__':main()
