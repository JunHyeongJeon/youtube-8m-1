# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Binary for generating error rates over a set of videos."""

import os
import gc
import time

import eval_util
import losses
import frame_level_models
import video_level_models
import data_augmentation
import feature_transform
import readers
import utils

import numpy
import tensorflow as tf
from tensorflow import app
from tensorflow import flags
from tensorflow import gfile
from tensorflow import logging

FLAGS = flags.FLAGS

if __name__ == '__main__':
  flags.DEFINE_string("train_dir", "/tmp/yt8m_model/",
                      "The directory to load the model files from.")
  flags.DEFINE_string("model_checkpoint_path", None,
                      "The file path to load the model from.")
  flags.DEFINE_string("output_file", "",
                      "The file to save the predictions to.")
  flags.DEFINE_string(
      "input_data_pattern", "",
      "File glob defining the evaluation dataset in tensorflow.SequenceExample "
      "format. The SequenceExamples are expected to have an 'rgb' byte array "
      "sequence feature as well as a 'labels' int64 context feature.")

  # Model flags.
  flags.DEFINE_bool(
      "frame_features", False,
      "If set, then --eval_data_pattern must be frame-level features. "
      "Otherwise, --eval_data_pattern must be aggregated video-level "
      "features. The model must also be set appropriately (i.e. to read 3D "
      "batches VS 4D batches.")
  flags.DEFINE_integer(
      "batch_size", 8192,
      "How many examples to process per batch.")
  flags.DEFINE_string("feature_names", "mean_rgb", "Name of the feature "
                      "to use for training.")
  flags.DEFINE_string("feature_sizes", "1024", "Length of the feature vectors.")
  flags.DEFINE_string(
      "model", "YouShouldSpecifyAModel",
      "Which architecture to use for the model. Models are defined "
      "in models.py.")

  # Other flags.
  flags.DEFINE_integer("num_readers", 2,
                       "How many threads to use for reading input files.")
  flags.DEFINE_integer("top_k", 20,
                       "How many predictions to output per video.")

  flags.DEFINE_bool(
      "dropout", False,
      "Whether to consider dropout")
  flags.DEFINE_float("keep_prob", 1.0, 
      "probability to keep output (used in dropout, keep it unchanged in validationg and test)")
  flags.DEFINE_float("noise_level", 0.0, 
      "standard deviation of noise (added to hidden nodes)")


def find_class_by_name(name, modules):
  """Searches the provided modules for the named class and returns it."""
  modules = [getattr(module, name, None) for module in modules]
  return next(a for a in modules if a)

def format_lines(video_ids, predictions, labels):
  batch_size = len(video_ids)
  for video_index in range(batch_size):
    # error rate
    top_k = max(int(numpy.sum(labels[video_index])), 1)
    top_indices = numpy.argpartition(predictions[video_index], -top_k)[-top_k:]
    positives = [labels[video_index][class_index] for class_index in top_indices]
    perr = sum(positives) / float(top_k)
    yield video_ids[video_index].decode('utf-8') + "\t" + str(1-perr) + "\n"

def get_input_data_tensors(reader, data_pattern, batch_size, num_readers=1):
  """Creates the section of the graph which reads the input data.

  Args:
    reader: A class which parses the input data.
    data_pattern: A 'glob' style path to the data files.
    batch_size: How many examples to process at a time.
    num_readers: How many I/O threads to use.

  Returns:
    A tuple containing the features tensor, labels tensor, and optionally a
    tensor containing the number of frames per video. The exact dimensions
    depend on the reader being used.

  Raises:
    IOError: If no files matching the given pattern were found.
  """
  with tf.name_scope("input"):
    files = gfile.Glob(data_pattern)
    if not files:
      raise IOError("Unable to find input files. data_pattern='" +
                    data_pattern + "'")
    logging.info("number of input files: " + str(len(files)))
    filename_queue = tf.train.string_input_producer(
        files, num_epochs=1, shuffle=False)
    examples_and_labels = [reader.prepare_reader(filename_queue)
                           for _ in range(num_readers)]

    video_id_batch, video_batch, labels_batch, num_frames_batch = (
        tf.train.batch_join(examples_and_labels,
                            batch_size=batch_size,
                            capacity=batch_size * 3,
                            allow_smaller_final_batch = True,
                            enqueue_many=True))
    return video_id_batch, video_batch, labels_batch, num_frames_batch
    

def build_graph(reader,
                model,
                input_data_pattern,
                label_loss_fn=losses.CrossEntropyLoss(),
                batch_size=1000,
                transformer_class=feature_transform.DefaultTransformer):
  
  video_id, model_input_raw, labels_batch, num_frames = (
      get_input_data_tensors(
          reader,
          input_data_pattern,
          batch_size=batch_size,
          num_readers=FLAGS.num_readers))

  feature_transformer = transformer_class()
  model_input, num_frames = feature_transformer.transform(model_input_raw, num_frames=num_frames)

  with tf.name_scope("model"):
    if FLAGS.noise_level > 0:
      noise_level_tensor = tf.placeholder_with_default(0.0, shape=[], name="noise_level")
    else:
      noise_level_tensor = None

    if FLAGS.dropout:
      keep_prob_tensor = tf.placeholder_with_default(1.0, shape=[], name="keep_prob")
      result = model.create_model(
          model_input,
          num_frames=num_frames,
          vocab_size=reader.num_classes,
          labels=labels_batch,
          dropout=FLAGS.dropout,
          keep_prob=keep_prob_tensor,
          noise_level=noise_level_tensor)
    else:
      result = model.create_model(
          model_input,
          num_frames=num_frames,
          vocab_size=reader.num_classes,
          labels=labels_batch,
          noise_level=noise_level_tensor)

    print "result", result
    predictions = result["predictions"]

    tf.add_to_collection("predictions", predictions)
    tf.add_to_collection("video_id_batch", video_id)
    tf.add_to_collection("input_batch_raw", model_input_raw)
    tf.add_to_collection("input_batch", model_input)
    tf.add_to_collection("num_frames", num_frames)
    tf.add_to_collection("labels", tf.cast(labels_batch, tf.float32))
    if FLAGS.dropout:
      tf.add_to_collection("keep_prob", keep_prob_tensor)
    if FLAGS.noise_level > 0:
      tf.add_to_collection("noise_level", noise_level_tensor)


def inference(saver, train_dir, out_file_location, batch_size, top_k):

  with tf.Session() as sess, open(out_file_location, "w") as out_file:
    
    if FLAGS.model_checkpoint_path:
      latest_checkpoint = FLAGS.model_checkpoint_path
    else:
      latest_checkpoint = tf.train.latest_checkpoint(train_dir)

    if latest_checkpoint is None:
      raise Exception("unable to find a checkpoint at location: %s" % train_dir)

    logging.info("restoring variables from " + latest_checkpoint)
    saver.restore(sess, latest_checkpoint)

    input_tensor = tf.get_collection("input_batch_raw")[0]
    num_frames_tensor = tf.get_collection("num_frames")[0]
    predictions_tensor = tf.get_collection("predictions")[0]
    video_id_tensor = tf.get_collection("video_id_batch")[0]
    labels_tensor = tf.get_collection("labels")[0]
    init_op = tf.global_variables_initializer()

    # Workaround for num_epochs issue.
    def set_up_init_ops(variables):
      init_op_list = []
      for variable in list(variables):
        if "train_input" in variable.name:
          init_op_list.append(tf.assign(variable, 1))
          variables.remove(variable)
      init_op_list.append(tf.variables_initializer(variables))
      return init_op_list

    sess.run(set_up_init_ops(tf.get_collection_ref(
        tf.GraphKeys.LOCAL_VARIABLES)))

    coord = tf.train.Coordinator()
    threads = tf.train.start_queue_runners(sess=sess, coord=coord)
    num_examples_processed = 0
    start_time = time.time()
    out_file.write("VideoId,LabelConfidencePairs\n")

    try:
      while not coord.should_stop():

        # for speed reason, here we use the graph, instead of using feed_dict and numpy arrays
        predictions_val, video_id_batch_val, labels_batch_val = sess.run([predictions_tensor, video_id_tensor, labels_tensor])

        num_examples_processed += len(video_id_batch_val)
        num_classes = predictions_val.shape[1]

        out_file.writelines([line for line in format_lines(video_id_batch_val, predictions_val, labels_batch_val)])

        now = time.time()
        logging.info("num examples processed: " + str(num_examples_processed) + " elapsed seconds: " + "{0:.2f}".format(now-start_time))

    except tf.errors.OutOfRangeError:
        logging.info('Done with inference. The output file was written to ' + out_file_location)
    finally:
        coord.request_stop()

    out_file.flush()
    coord.join(threads)
    sess.close()


def main(unused_argv):
  logging.set_verbosity(tf.logging.INFO)

  # convert feature_names and feature_sizes to lists of values
  feature_names, feature_sizes = utils.GetListOfFeatureNamesAndSizes(
      FLAGS.feature_names, FLAGS.feature_sizes)

  if FLAGS.frame_features:
    reader = readers.YT8MFrameFeatureReader(feature_names=feature_names,
                                            feature_sizes=feature_sizes)
  else:
    reader = readers.YT8MAggregatedFeatureReader(feature_names=feature_names,
                                                 feature_sizes=feature_sizes)

  if FLAGS.output_file is "":
    raise ValueError("'output_file' was not specified. "
      "Unable to continue with inference.")

  if FLAGS.input_data_pattern is "":
    raise ValueError("'input_data_pattern' was not specified. "
      "Unable to continue with inference.")

  model = find_class_by_name(FLAGS.model,
                             [frame_level_models, video_level_models])()
  transformer_fn = find_class_by_name(FLAGS.feature_transformer, 
                                         [feature_transform])

  build_graph(reader,
              model,
              input_data_pattern=FLAGS.input_data_pattern,
              batch_size=FLAGS.batch_size,
              transformer_class=transformer_fn)

  saver = tf.train.Saver(max_to_keep=3, keep_checkpoint_every_n_hours=10000000000)

  inference(saver, FLAGS.train_dir,
            FLAGS.output_file, FLAGS.batch_size, FLAGS.top_k)


if __name__ == "__main__":
  app.run()
