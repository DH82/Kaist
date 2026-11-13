import importlib.util,sys
from sam_progress import install

def main():
 path=sys.argv.pop(1)
 spec=importlib.util.spec_from_file_location('baseline_sam',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 install(module);module.main()
if __name__=='__main__':main()
