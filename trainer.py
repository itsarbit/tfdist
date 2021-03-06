import tensorflow as tf
from tensorflow.examples.tutorials.mnist import input_data

import math
import time

# Flags for defining the tf.train.ClusterSpec
tf.app.flags.DEFINE_string("ps_hosts", "",
        "Comma-separated list of hostname:port pairs")
tf.app.flags.DEFINE_string("worker_hosts", "",
        "Comma-separated list of hostname:port pairs")

# Flags for defining the tf.train.Server
flags = tf.app.flags
flags.DEFINE_string("job_name", "", "One of 'ps', 'worker'")
flags.DEFINE_integer("task_index", 0, "Index of task within the job")
flags.DEFINE_integer("train_steps", 1000,
                     "Number of (global) training steps to perform")
flags.DEFINE_float('learning_rate', 0.01, 'Initial learning rate.')
flags.DEFINE_integer('batch_size', 100, 'Batch size.  '
        'Must divide evenly into the dataset sizes.')
flags.DEFINE_string('train_dir', '/tmp/tfdata',
        'Directory to put the training data.')
flags.DEFINE_string('log_dir', '/tmp/tflogs',
        'Directory to put the logs.')

flags.DEFINE_integer("replicas_to_aggregate", None,
                     "Number of replicas to aggregate before paramter update"
                     "is applied (For sync_replicas mode only; default: "
                     "num_workers)")
flags.DEFINE_boolean("sync_replicas", False,
                     "Use the sync_replicas (synchronized replicas) mode, "
                     "wherein the parameter updates from workersare aggregated "
                     "before applied to avoid stale gradients")

FLAGS = tf.app.flags.FLAGS

IMAGE_PIXELS = 28
flags.DEFINE_integer("hidden_units", 100,
                     "Number of units in the hidden layer of the NN")

def main(_):
    ps_hosts = FLAGS.ps_hosts.split(",")
    worker_hosts = FLAGS.worker_hosts.split(",")

    # Create a cluster from the parameter server and worker hosts.
    cluster = tf.train.ClusterSpec({"ps": ps_hosts, "worker": worker_hosts})

    print "Starting servers..."
    # Create and start a server for the local task.
    server = tf.train.Server(cluster,
            job_name=FLAGS.job_name,
            task_index=FLAGS.task_index)

    if FLAGS.sync_replicas:
        if FLAGS.replicas_to_aggregate is None:
            replicas_to_aggregate = len(worker_hosts)
        else:
            replicas_to_aggregate = FLAGS.replicas_to_aggregate

    if FLAGS.job_name == "ps":
        server.join()
    elif FLAGS.job_name == "worker":
        # Assigns ops to the local worker by default.
        with tf.device(tf.train.replica_device_setter(
            worker_device="/job:worker/task:%d" % FLAGS.task_index,
            cluster=cluster)):

            is_chief=(FLAGS.task_index == 0)

            mnist = input_data.read_data_sets(FLAGS.train_dir, one_hot=True)
            # If True also add the variable to the graph collection
            # GraphKeys.TRAINABLE_VARIABLES.
            global_step = tf.Variable(0, name="global_step", trainable=False)
            # Variables of the hidden layer
            hid_w = tf.Variable(
                tf.truncated_normal([IMAGE_PIXELS * IMAGE_PIXELS, FLAGS.hidden_units],
                                    stddev=1.0 / IMAGE_PIXELS), name="hid_w")
            hid_b = tf.Variable(tf.zeros([FLAGS.hidden_units]), name="hid_b")

            # Variables of the softmax layer
            sm_w = tf.Variable(
                tf.truncated_normal([FLAGS.hidden_units, 10],
                                    stddev=1.0 / math.sqrt(FLAGS.hidden_units)),
                name="sm_w")
            sm_b = tf.Variable(tf.zeros([10]), name="sm_b")

            x = tf.placeholder(tf.float32, [None, IMAGE_PIXELS * IMAGE_PIXELS])
            y_ = tf.placeholder(tf.float32, [None, 10])

            hid_lin = tf.nn.xw_plus_b(x, hid_w, hid_b)
            hid = tf.nn.relu(hid_lin)

            y = tf.nn.softmax(tf.nn.xw_plus_b(hid, sm_w, sm_b))
            cross_entropy = -tf.reduce_sum(y_ *
                                           tf.log(tf.clip_by_value(y, 1e-10, 1.0)))

            opt = tf.train.AdamOptimizer(FLAGS.learning_rate)

            if FLAGS.sync_replicas:
                opt = tf.train.SyncReplicasOptimizer(
                        opt,
                        replicas_to_aggregate=replicas_to_aggregate,
                        total_num_replicas=len(worker_hosts),
                        replica_id=FLAGS.task_index,
                        name="mnist_sync_replicas")

            train_step = opt.minimize(cross_entropy,
                    global_step=global_step)

            if FLAGS.sync_replicas and is_chief:
                # Initial token and chief queue runners required by the sync_replicas mode
                chief_queue_runner = opt.get_chief_queue_runner()
                init_tokens_op = opt.get_init_tokens_op()

            init_op = tf.initialize_all_variables()

            saver = tf.train.Saver()
            summary_op = tf.merge_all_summaries()
            init_op = tf.initialize_all_variables()

        # Create a "supervisor", which oversees the training process.
        sv = tf.train.Supervisor(is_chief=is_chief,
                logdir=FLAGS.log_dir,
                init_op=init_op,
                summary_op=summary_op,
                saver=saver,
                global_step=global_step,
                recovery_wait_secs=1)

        sess_config = tf.ConfigProto(
                allow_soft_placement=True,
                log_device_placement=True,
                device_filters=["/job:ps", "/job:worker/task:%d" % FLAGS.task_index])

        print("Worker %d: Session start initializing..." % FLAGS.task_index)

        # The supervisor takes care of session initialization and restoring from
        # a checkpoint.
        sess = sv.prepare_or_wait_for_session(server.target, config=sess_config)

        if FLAGS.sync_replicas and is_chief:
            # Chief worker will start the chief queue runner and call the init op
            print("Starting chief queue runner and running init_tokens_op")
            sv.start_queue_runners(sess, [chief_queue_runner])
            sess.run(init_tokens_op)

        print("Worker %d: Session initialization complete." % FLAGS.task_index)

        # Start queue runners for the input pipelines (if any).
        # Perform training
        time_begin = time.time()
        print("Training begins @ %f" % time_begin)

        # Loop until the supervisor shuts down (or 1000000 steps have completed).
        local_step = 0
        while True:
            # Run a training step asynchronously.
            # See `tf.train.SyncReplicasOptimizer` for additional details on how to
            # perform *synchronous* training.
            batch_xs, batch_ys = mnist.train.next_batch(FLAGS.batch_size)
            train_feed = {x: batch_xs,
                          y_: batch_ys}

            _, step = sess.run([train_step, global_step], feed_dict=train_feed)
            local_step += 1

            now = time.time()
            print("%f: Worker %d: training step %d done (global step: %d)" %
                  (now, FLAGS.task_index, local_step, step))

            if step >= FLAGS.train_steps:
              break

        time_end = time.time()
        print("Training ends @ %f" % time_end)
        training_time = time_end - time_begin
        print("Training elapsed time: %f s" % training_time)

        # Validation feed
        val_feed = {x: mnist.validation.images,
                    y_: mnist.validation.labels}
        val_xent = sess.run(cross_entropy, feed_dict=val_feed)
        print("After %d training step(s), validation cross entropy = %g" %
              (FLAGS.train_steps, val_xent))


if __name__ == "__main__":
    tf.app.run()
