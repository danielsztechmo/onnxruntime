import csv
import timeit
from datetime import datetime
import numpy
import logging
import coloredlogs
import numpy as np
import argparse

from BERTSquad import *
from Resnet50 import *
from FastRCNN import *
from MaskRCNN import *
from SSD import *

logger = logging.getLogger('')

MODELS = {
    "bert-squad": (BERTSquad, "bert-squad"),
    "resnet50": (Resnet50, "resnet50"),
    "fast-rcnn": (FastRCNN, "fast-rcnn"),
    "mask-rcnn": (MaskRCNN, "mask-rcnn"),
    "ssd": (SSD, "ssd"),
}

def get_latency_result(runtimes, batch_size):
    latency_ms = sum(runtimes) / float(len(runtimes)) * 1000.0
    latency_variance = numpy.var(runtimes, dtype=numpy.float64) * 1000.0
    throughput = batch_size * (1000.0 / latency_ms)

    return {
        "test_times": len(runtimes),
        "latency_variance": "{:.2f}".format(latency_variance),
        "latency_90_percentile": "{:.2f}".format(numpy.percentile(runtimes, 90) * 1000.0),
        "latency_95_percentile": "{:.2f}".format(numpy.percentile(runtimes, 95) * 1000.0),
        "latency_99_percentile": "{:.2f}".format(numpy.percentile(runtimes, 99) * 1000.0),
        "average_latency_ms": "{:.2f}".format(latency_ms),
        "QPS": "{:.2f}".format(throughput),
    }

def inference_ort(ort_session, inputs, result_template, repeat_times, batch_size):
    result = {}
    runtimes = timeit.repeat(lambda: ort_session.inference(inputs), number=1, repeat=repeat_times)
    result.update(result_template)
    result.update({"io_binding": False})
    result.update(get_latency_result(runtimes, batch_size))
    return result

def get_cuda_version():
    p = subprocess.Popen(["cat", "/usr/local/cuda/version.txt"], stdout=subprocess.PIPE) # (stdout, stderr)
    stdout, sterr = p.communicate()
    stdout = stdout.decode("ascii").strip()
    
    return stdout

def get_trt_version():
    p1 = subprocess.Popen(["dpkg", "-l"], stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["grep", "TensorRT runtime libraries"], stdin=p1.stdout, stdout=subprocess.PIPE)
    stdout, sterr = p2.communicate()
    stdout = stdout.decode("ascii").strip()
    
    if stdout != "":
        import re
        stdout = re.sub('\s+', ' ', stdout)
        return stdout 

    if os.path.exists("/usr/lib/x86_64-linux-gnu/libnvinfer.so"):
        p1 = subprocess.Popen(["readelf", "-s", "/usr/lib/x86_64-linux-gnu/libnvinfer.so"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep", "version"], stdin=p1.stdout, stdout=subprocess.PIPE)
        stdout, sterr = p2.communicate()
        stdout = stdout.decode("ascii").strip()
        stdout = stdout.split(" ")[-1]
        return stdout

    elif os.path.exists("/usr/lib/aarch64-linux-gnu/libnvinfer.so"):
        p1 = subprocess.Popen(["readelf", "-s", "/usr/lib/aarch64-linux-gnu/libnvinfer.so"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["grep", "version"], stdin=p1.stdout, stdout=subprocess.PIPE)
        stdout, sterr = p2.communicate()
        stdout = stdout.decode("ascii").strip()
        stdout = stdout.split(" ")[-1]
        return stdout
    
    return ""

"""
"""
def load_onnx_model_zoo_test_data(path):
    p1 = subprocess.Popen(["find", path, "-name", "test_data_set*", "-type", "d"], stdout=subprocess.PIPE)
    p2 = subprocess.Popen(["sort"], stdin=p1.stdout, stdout=subprocess.PIPE)
    stdout, sterr = p2.communicate()
    stdout = stdout.decode("ascii").strip()
    test_data_set_dir = stdout.split("\n") 
    print(stdout)
    print(test_data_set_dir)

    inputs = []
    outputs = []

    # find test data path
    for test_data_dir in test_data_set_dir:
        pwd = os.getcwd()
        os.chdir(test_data_dir)

        p1 = subprocess.Popen(["find", ".", "-name", "input_*"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["sort"], stdin=p1.stdout, stdout=subprocess.PIPE)
        stdout, sterr = p2.communicate()
        stdout = stdout.decode("ascii").strip()
        input_data = stdout.split("\n") 
        print(input_data)

        p1 = subprocess.Popen(["find", ".", "-name", "output_*"], stdout=subprocess.PIPE)
        p2 = subprocess.Popen(["sort"], stdin=p1.stdout, stdout=subprocess.PIPE)
        stdout, sterr = p2.communicate()
        stdout = stdout.decode("ascii").strip()
        output_data = stdout.split("\n") 
        print(output_data)

        # load inputs
        input_data_pb = [] 
        for data in input_data:
            tensor = onnx.TensorProto()
            with open(data, 'rb') as f:
                tensor.ParseFromString(f.read())
                input_data_pb.append(numpy_helper.to_array(tensor))
        inputs.append(input_data_pb)

        # load outputs 
        output_data_pb = [] 
        for data in output_data:
            tensor = onnx.TensorProto()
            with open(data, 'rb') as f:
                tensor.ParseFromString(f.read())
                output_data_pb.append(numpy_helper.to_array(tensor))
        outputs.append(output_data_pb)

        os.chdir(pwd)

    print('Loaded {} inputs successfully.'.format(len(inputs)))
    print('Loaded {} outputs successfully.'.format(len(outputs)))

    return inputs, outputs

def validate(all_ref_outputs, all_outputs, decimal):
    print('Reference {} results.'.format(len(all_ref_outputs)))
    print('Predicted {} results.'.format(len(all_outputs)))
    # print(np.array(all_ref_outputs).shape)
    # print(np.array(all_outputs).shape)
    # print(all_ref_outputs)
    # print(all_outputs)

    for i in range(len(all_outputs)):
        ref_outputs = all_ref_outputs[i]
        outputs = all_outputs[i]

        for j in range(len(outputs)):
            ref_output = ref_outputs[j]
            output = outputs[j]
            # print(ref_output)
            # print(output)

            # Compare the results with reference outputs up to x decimal places
            for ref_o, o in zip(ref_output, output):
                np.testing.assert_almost_equal(ref_o, o, decimal)

    print('ONNX Runtime outputs are similar to reference outputs!')


def run_onnxruntime(models=MODELS):
    import onnxruntime

    results = []
    for name in models.keys():
        info = models[name] 
        model_class = info[0]
        path = info[1]

        pwd = os.getcwd()
        if not os.path.exists(path):
            os.mkdir(path)
        os.chdir(path)

        # for ep in ["TensorrtExecutionProvider", "CUDAExecutionProvider"]:
        for ep in ["CUDAExecutionProvider"]:
            if (ep not in onnxruntime.get_available_providers()):
                logger.error("No {} support".format(ep))
                continue

            # these settings are temporary
            fp16 = False
            sequence_length = 1
            optimize_onnx = False
            repeat_times = 5 
            batch_size = 1
            device_info = [] 

            # create onnxruntime inference session
            logger.info("Initializing {} with {}...".format(name, ep))

            model = model_class()
            sess = model.get_session()

            test_set_dir = model.get_onnx_zoo_test_data_dir()
            inputs, ref_outputs = load_onnx_model_zoo_test_data(test_set_dir)

            if ep == "CUDAExecutionProvider":
                sess.set_providers([ep])
                device_info.append(get_cuda_version())
            elif ep == "TensorrtExecutionProvider":
                device_info.append(get_cuda_version())
                device_info.append(get_trt_version())

            result_template = {
                "engine": "onnxruntime",
                "version": onnxruntime.__version__,
                "device": ep,
                "device_info": ','.join(device_info),
                "optimizer": optimize_onnx,
                "fp16": fp16,
                "io_binding": False,
                "model_name": model.get_model_name(),
                "inputs": len(sess.get_inputs()),
                "batch_size": batch_size,
                "sequence_length": sequence_length,
                "datetime": str(datetime.now()),
            }

            logger.info(sess.get_providers())
            logger.info("Inferencing {} with {} ...".format(model.get_model_name(), ep))

            result = inference_ort(model, inputs, result_template, repeat_times, batch_size)

            validate(ref_outputs, model.get_outputs(), model.get_decimal())

            logger.info(result)
            results.append(result)
            #model.postprocess()

        os.chdir(pwd)

    return results

def output_details(results, csv_filename):
    with open(csv_filename, mode="a", newline='') as csv_file:
        column_names = [
            "engine", "version", "device", "device_info", "fp16", "optimizer", "io_binding", "model_name", "inputs", "batch_size",
            "sequence_length", "datetime", "test_times", "QPS", "average_latency_ms", "latency_variance",
            "latency_90_percentile", "latency_95_percentile", "latency_99_percentile"
        ]

        csv_writer = csv.DictWriter(csv_file, fieldnames=column_names)
        csv_writer.writeheader()
        for result in results:
            csv_writer.writerow(result)

    logger.info(f"Detail results are saved to csv file: {csv_filename}")

def parse_arguments():
    parser = argparse.ArgumentParser()

    parser.add_argument("-d", "--detail_csv", required=False, default=None, help="CSV file for saving detail results.")

    # parser.add_argument("-r", "--result_csv", required=False, default=None, help="CSV file for saving summary results.")

    args = parser.parse_args()
    return args

def setup_logger(verbose):
    if verbose:
        coloredlogs.install(level='DEBUG', fmt='[%(filename)s:%(lineno)s - %(funcName)20s()] %(message)s')
    else:
        coloredlogs.install(fmt='%(message)s')
        logging.getLogger("transformers").setLevel(logging.WARNING)

def main():
    args = parse_arguments()
    setup_logger(False)

    results = run_onnxruntime()

    time_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_filename = args.detail_csv or f"benchmark_detail_{time_stamp}.csv"
    output_details(results, csv_filename)

    # csv_filename = args.result_csv or f"benchmark_summary_{time_stamp}.csv"
    # output_summary(results, csv_filename, args)


if __name__ == "__main__":
    main()