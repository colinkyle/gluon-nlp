import pytest
import os
import io
import numpy as np
import mxnet as mx
import gluonnlp as nlp

@pytest.mark.parametrize('num_workers', [0, 2])
def test_parallel(num_workers):
    class ParallelNet(nlp.utils.Parallelizable):
        def __init__(self, net, loss):
            self._net = net
            self._loss = loss

        def forward_backward(self, x):
            data, label = x
            with mx.autograd.record():
                out = self._net(data)
                loss = self._loss(out, label)
            loss.backward()
            return loss
    # model
    net = mx.gluon.nn.Dense(2, prefix='test_parallel_')
    loss = mx.gluon.loss.SoftmaxCELoss()
    ctxs = [mx.cpu(0), mx.cpu(1)]
    net.initialize(ctx=ctxs)
    params = net.collect_params()

    # parallel model
    para_net = ParallelNet(net, loss)
    parallel = nlp.utils.Parallel(num_workers, para_net)

    # sample data
    data = mx.nd.random.uniform(shape=(2,5))
    label = mx.nd.array([[0], [1]])
    data_list = mx.gluon.utils.split_and_load(data, ctxs)
    label_list = mx.gluon.utils.split_and_load(label, ctxs)

    # train parallel
    epoch = 2
    params.zero_grad()
    params.setattr('req', 'add')
    parallel_loss = 0
    for i in range(epoch):
        for x, y in zip(data_list, label_list):
            parallel.put((x,y))
        for x, y in zip(data_list, label_list):
            ls = parallel.get()
            parallel_loss += ls.asscalar()

    grads = params['test_parallel_weight'].list_grad()
    parallel_grads_np = [grad.asnumpy() for grad in grads]

    # train serial
    params.zero_grad()
    params.setattr('req', 'add')
    serial_loss = 0
    for i in range(epoch):
        with mx.autograd.record():
            for x, y in zip(data_list, label_list):
                ls = loss(net(x), y)
                ls.backward()
                serial_loss += ls.asscalar()

    grads = params['test_parallel_weight'].list_grad()
    serial_grads_np = [grad.asnumpy() for grad in grads]
    assert serial_loss == parallel_loss
    for para_grad, serial_grad in zip(parallel_grads_np, serial_grads_np):
        mx.test_utils.assert_almost_equal(para_grad, serial_grad)

@pytest.mark.parametrize('max_norm,check_isfinite',
                         [(1, True),
                          (1, False),
                          (3, True),
                          (3, False)])
def test_clip_grad_norm(max_norm, check_isfinite):
    contexts = [mx.cpu(0), mx.cpu(1)]
    net = mx.gluon.nn.Dense(1, weight_initializer='ones', bias_initializer='ones')
    net.initialize(ctx=contexts)
    net.hybridize()
    trainer = mx.gluon.Trainer(net.collect_params(), 'sgd', update_on_kvstore=False)
    for ctx in contexts:
        with mx.autograd.record():
            out = net(mx.nd.ones((1, 1), ctx=ctx))
        out.backward()
    trainer.allreduce_grads()
    with mx.cpu(2):
        norm = nlp.utils.clip_grad_global_norm(net.collect_params().values(),
                                               max_norm, check_isfinite)
    if isinstance(norm, mx.nd.NDArray):
        norm = norm.asnumpy()
    mx.test_utils.assert_almost_equal(norm, np.sqrt(8), atol=1e-5)
    for ctx in contexts:
        if max_norm > np.sqrt(8): # no clipping
            assert net.weight.grad(ctx).reshape(-1) == 2
            assert net.bias.grad(ctx).reshape(-1) == 2
        else:
            assert net.weight.grad(ctx).reshape(-1) < 2
            assert net.bias.grad(ctx).reshape(-1) < 2

@pytest.mark.parametrize('filename', ['net.params', './net.params'])
def test_save_parameters(filename):
    net = mx.gluon.nn.Dense(1, in_units=1)
    net.initialize()
    nlp.utils.save_parameters(net, filename)
    nlp.utils.load_parameters(net, filename)

@pytest.mark.parametrize('filename', ['net.states', './net.states'])
def test_save_states(filename):
    net = mx.gluon.nn.Dense(1, in_units=1)
    net.initialize()
    trainer = mx.gluon.Trainer(net.collect_params(), 'sgd',
                               update_on_kvstore=False)
    nlp.utils.save_states(trainer, filename)
    assert os.path.isfile(filename)
    nlp.utils.load_states(trainer, filename)

@pytest.mark.parametrize('dirname', ['~/dir1', '~/dir1/dir2'])
def test_mkdir(dirname):
    nlp.utils.mkdir(dirname)
    assert os.path.isdir(os.path.expanduser(dirname))

def test_glob():
    f0 = io.open('test_glob_00', 'w')
    f1 = io.open('test_glob_01', 'w')
    f2 = io.open('test_glob_11', 'w')
    files = nlp.utils.glob('test_glob_0*,test_glob_1*')
    assert len(files) == 3
    files_fake = nlp.utils.glob('fake_glob')
    assert len(files_fake) == 0

def test_version():
    future_version = '10.11.12'
    past_version = '0.1.2'
    with pytest.raises(AssertionError):
        nlp.utils.check_version(future_version, warning_only=False)
    with pytest.raises(UserWarning):
        nlp.utils.check_version(future_version, warning_only=True)
    nlp.utils.check_version(past_version, warning_only=False)
    nlp.utils.check_version(past_version, warning_only=True)

def test_train_valid_split():
    # Create test set
    data ={}
    data['texts'] = ['this is','the test','dataset for',
                     'train_valid_split','the function',
                     'for splitting','data into',
                     'a validation','set and','a training',
                     'set including','stratify option']
    data['labels'] = [1,2,3,1,2,3,3,3,3,3,2,2]

    # Create a list of review a label pairs
    dataset = [[text, int(label)] for text, label in zip(data['texts'], data['labels'])]
    classes,digitized = np.unique(data['labels'],return_inverse=True)
    n_classes = len(classes)
    num_class = np.bincount(digitized)

    train_dataset, valid_dataset = nlp.data.train_valid_split(dataset)

    assert (len(valid_dataset) == np.ceil(.05*len(dataset)).astype(int)) and \
           (len(train_dataset)+len(valid_dataset) == len(dataset))

    train_dataset, valid_dataset = nlp.data.train_valid_split(dataset,stratify=data['labels'])

    valid_labels = [d[1] for d in valid_dataset]
    valid_num_class = [np.sum(valid_labels==classes[i]) for i in range(len(classes))]

    assert np.all(np.ceil(.05*num_class).astype(int) == valid_num_class) and \
           (len(train_dataset) + len(valid_dataset) == len(dataset))
