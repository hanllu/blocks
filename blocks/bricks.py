"""Bricks module

This defines the basic interface of bricks.
"""
import copy
import types
from abc import ABCMeta
from functools import wraps, partial
import inspect
import logging
from collections import OrderedDict

import numpy as np
import theano
from theano import tensor

from blocks.utils import (pack, reraise_as, shared_floatx_zeros, unpack,
                          check_theano_variable)
from blocks.initialization import Constant

BRICK_PREFIX = 'brick_'
INPUT_SUFFIX = '_input'
OUTPUT_SUFFIX = '_output'
DEFAULT_SEED = [2014, 10, 5]
PARAM_OWNER_TAG = 'param_owner'

logging.basicConfig()
logger = logging.getLogger(__name__)


class Brick(object):
    """A brick encapsulates Theano operations with parameters.

    A brick goes through the following stages:

    1. Construction: The call to :meth:`__init__` constructs a
       :class:`Brick` instance with a name and creates any child bricks as
       well.
    2. Allocation of parameters:

       a) Allocation configuration of children: The
          :meth:`push_allocation_config` method configures any children of
          this block.
       b) Allocation: The :meth:`allocate` method allocates the shared
          Theano variables required for the parameters. Also allocates
          parameters for all children.

    3. The following can be done in either order:

       a) Application: By applying the brick to a set of Theano
          variables a part of the computational graph of the final model is
          constructed.
       b) The initialization of parameters:

          1. Initialization configuration of children: The
             :meth:`push_initialization_config` method configures any
             children of this block.
          2. Initialization: This sets the initial values of the
             parameters by a call to :meth:`initialize`, which is needed
             to call the final compiled Theano function.  Also initializes
             all children.

    Not all stages need to be called explicitly. Step 3(a) will
    automatically allocate the parameters if needed. Similarly, step
    3(b.2) and 2(b) will automatically perform steps 3(b.1) and 2(a) if
    needed. They only need to be called separately if greater control is
    required. The only two methods which always need to be called are an
    application method to construct the computational graph, and the
    :meth:`initialize` method in order to initialize the parameters.

    At each different stage, a brick might need a certain set of
    configuration settings. All of these settings can be passed to the
    :meth:`__init__` constructor. However, by default many bricks support
    *lazy initialization*. This means that the configuration settings can
    be set later.

    .. note::

       Some arguments to :meth:`__init__` are *always* required, even when
       lazy initialization is enabled. Other arguments must be given before
       calling :meth:`allocate`, while others yet only need to be given in
       order to call :meth:`initialize`. Always read the documentation of
       each brick carefully.

    Lazy initialization can be turned off by setting ``Brick.lazy =
    False``. In this case, there is no need to call :meth:`initialize`
    manually anymore, but all the configuration must be passed to the
    :meth:`__init__` method.

    Parameters
    ----------
    name : str, optional
        The name of this brick. This can be used to filter the application
        of certain modifications by brick names. By default, the brick
        receives the name of its class (lowercased).

    Attributes
    ----------
    name : str
        The name of this brick.
    lazy : bool
        ``True`` by default. When bricks are lazy, not all configuration
        needs to be provided to the constructor, allowing it to be set in
        another way after construction. Many parts of the library rely on
        this behaviour. However, it does require a separate call to
        :meth:`initialize`. If set to ``False`` on the other hand, bricks
        will be ready to run after construction.
    params : list of Theano shared variables
        After calling the :meth:`allocate` method this attribute will be
        populated with the shared variables storing this brick's
        parameters.
    children : list of bricks
        The children of this brick.
    allocated : bool
        ``False`` if :meth:`allocate` has not been called yet. ``True``
        otherwise.
    initialized : bool
        ``False`` if :meth:`allocate` has not been called yet. ``True``
        otherwise.
    allocation_config_pushed : bool
        ``False`` if :meth:`allocate` or :meth:`push_allocation_config`
        hasn't been called yet. ``True`` otherwise.
    initialization_config_pushed : bool
        ``False`` if :meth:`initialize` or
        :meth:`push_initialization_config` hasn't been called yet. ``True``
        otherwise.

    Notes
    -----
    To provide support for lazy initialization, apply the :meth:`lazy`
    decorator to the :meth:`__init__` method.

    Brick implementations *must* call the :meth:`__init__` constructor of
    their parent using `super(BlockImplementation,
    self).__init__(**kwargs)` at the *beginning* of the overriding
    `__init__`.

    The methods :meth:`_allocate` and :meth:`_initialize` need to be
    overridden if the brick needs to allocate shared variables and
    initialize their values in order to function.

    A brick can have any number of methods which apply the brick on Theano
    variables. These methods should be decorated with the
    :meth:`apply_method` decorator.

    If a brick has children, they must be listed in the :attr:`children`
    attribute. Moreover, if the brick wants to control the configuration of
    its children, the :meth:`_push_allocation_config` and
    :meth:`_push_initialization_config` methods need to be overridden.

    Examples
    --------
    By default, bricks have lazy initialization enabled.

    >>> import theano
    ... from blocks.initialization import IsotropicGaussian, Constant
    ... linear = Linear(input_dim=5, weights_init=IsotropicGaussian(),
    ...                 biases_init=Constant(0))
    ... x = theano.tensor.vector()
    ... linear.apply(x)  # Calls linear.allocate() automatically
    ... linear.output_dim = 3
    ... linear.initialize()

    In simple cases, eager bricks are easier to deal with.

    >>> from blocks.initialization import IsotropicGaussian, Constant
    ... Brick.lazy = False
    ... linear = Linear(5, 3, weights_init=IsotropicGaussian(),
    ...                 biases_init=Constant(0))
    ... linear.apply(x)

    """
    __metaclass__ = ABCMeta
    #: See :attr:`lazy`
    lazy = True

    def __init__(self, name=None):
        if name is None:
            name = self.__class__.__name__.lower()
        self.name = '{}{}'.format(BRICK_PREFIX, name)

        self.children = []

        self.allocated = False
        self.allocation_config_pushed = False
        self.initialized = False
        self.initialization_config_pushed = False

    def __str__(self):
        return self.name

    def __getstate__(self):
        """Override the default __getstate__ method.

        Ensures that `params` are not copied even when `copy.deepcopy`
        is called by excluding them from the brick's state together
        with the attributes `allocated` and `initialized`.
        """
        state = self.__dict__
        for attr in ['allocated', 'initialized', 'params']:
            if attr in state:
                state.pop(attr)
        return state

    def __setstate__(self, state):
        """Override the default __setstate__ method.

        Calls the __init__ method before setting the state to ensure
        default initialization of the attributes excluded from the state
        in __getstate__.

        Parameters
        ----------
        state : dict
            Dictionary of the attributes to set.
        """
        Brick.__init__(self)
        self.__dict__.update(state)

    @staticmethod
    def _apply_method(pass_wrapper_reference):
        """Wraps an apply method.

        Parameters
        ----------
        pass_wrapper_reference : bool
            If True a reference to the wrapper object is passed to the apply
            method.

        """
        def decorator(func):
            @wraps(func, updated=())
            class ApplyWrapper(object):

                __metaclass__ = ABCMeta

                def __init__(self):
                    self.signature = lambda brick: ApplySignature()

                def __get__(self, brick, owner):
                    if brick:
                        def call_wrapper(brick, *args, **kwargs):
                            return self(brick, *args, **kwargs)
                        return types.MethodType(call_wrapper, brick, brick.__class__)
                    else:
                        return self

                def __call__(self, brick, *inputs, **kwargs):
                    return_list = kwargs.pop('return_list', False)
                    return_dict = kwargs.pop('return_dict', False)
                    assert not return_list or not return_dict

                    if not brick.allocated:
                        brick.allocate()
                    if not brick.initialized and not brick.lazy:
                        brick.initialize()
                    inputs = list(inputs)
                    for i, inp in enumerate(inputs):
                        if isinstance(inp, tensor.Variable):
                            inputs[i] = inp.copy()
                            inputs[i].tag.owner = brick
                            inputs[i].name = brick.name + INPUT_SUFFIX
                    for key, value in kwargs.items():
                        if isinstance(value, tensor.Variable):
                            kwargs[key] = value.copy()
                            kwargs[key].tag.owner = brick
                            kwargs[key].name = brick.name + INPUT_SUFFIX
                    if pass_wrapper_reference:
                        outputs = func(self, brick, *inputs, **kwargs)
                    else:
                        outputs = func(brick, *inputs, **kwargs)
                    # TODO: allow user to return an OrderedDict
                    outputs = pack(outputs)
                    for i, output in enumerate(outputs):
                        if isinstance(output, tensor.Variable):
                            # TODO Tag with dimensions, axes, etc. for
                            # error-checking
                            outputs[i] = output.copy()
                            outputs[i].tag.owner = brick
                            outputs[i].name = brick.name + OUTPUT_SUFFIX
                    if return_list:
                        return outputs
                    if return_dict:
                        return OrderedDict(
                            zip(self.signature(brick).output_names, outputs))
                    return unpack(outputs)

                def signature_method(self, signature_func):
                    self.signature = signature_func
                    return signature_func

            return ApplyWrapper()
        return decorator

    def signature(self, apply_method):
        """Returns the current signature of `apply_method`

        Parameters
        ----------
        apply_method : str
            Name of the apply method.

        """
        return getattr(self.__class__, apply_method).signature(self)

    @staticmethod
    def apply_method(func):
        """Wraps methods that apply a brick to inputs in different ways.

        This decorator will provide some necessary pre- and post-processing
        of the Theano variables, such as tagging them with the brick that
        created them and naming them. These changes will apply to
        Theano variables given as positional arguments and keywords arguments.

        .. warning::

            Properly set tags are important for correct functioning of the
            framework. Do not provide inputs to your apply method in a way
            different than passing them as positional or keyword arguments,
            e.g. as list or tuple elements.

        Application methods will allocate the brick parameters with a call
        :meth:`allocate` if they have not been allocated already.

        Parameters
        ----------
        func : method
            A method which takes Theano variables as an input, and returns
            the output of the Brick

        Raises
        ------
        LazyInitializationError
            If parameters needed to perform the application of this brick
            have not been provided yet.

        """
        return Brick._apply_method(False)(func)

    @staticmethod
    def lazy_method(func):
        """Makes the initialization lazy.

        Any positional argument not given will be set to ``None``.
        Positional arguments can also be given as keyword arguments.

        .. todo::

           Use ``UNDEF`` or ``None`` as a default?


        Parameters
        ----------
        func : method
            The __init__ method to make lazy.

        Examples
        --------

        >>> class SomeBrick(Brick):
        ...     @Brick.lazy_method
        ...     def __init__(self, a, b, c='c', d=None):
        ...         print a, b, c, d
        >>> brick = SomeBrick('a')
        a None c None
        >>> brick = SomeBrick(d='d', b='b')
        None b c d
        >>> Brick.lazy = False
        >>> brick = SomeBrick('a')
        Traceback (most recent call last):
          File "<stdin>", line 1, in <module>
        TypeError: __init__() takes at least 3 arguments (2 given)
        >>> Brick.lazy = True  # Reset for other doctests

        """
        arg_spec = inspect.getargspec(func)
        arg_names = arg_spec.args[1:]
        defaults = arg_spec.defaults
        if defaults is None:
            defaults = []

        def init(self, *args, **kwargs):
            if not self.lazy:
                return func(self, *args, **kwargs)
            # Fill any missing positional arguments with None
            args = args + (None,) * (len(arg_names) - len(defaults) -
                                     len(args))

            # Check if positional arguments were passed as keyword arguments
            args = list(args)
            for i, arg_name in enumerate(arg_names[:len(arg_names)
                                                    - len(defaults)]):
                if arg_name in kwargs:
                    if args[i] is not None:
                        raise ValueError
                    args[i] = kwargs.pop(arg_name)

            return func(self, *args, **kwargs)
        return init

    def allocate(self):
        """Allocate shared variables for parameters.

        Based on the current configuration of this :class:`Brick` create
        Theano shared variables to store the parameters.  After allocation,
        parameters are accessible through the :attr:`params` attribute.

        This method calls the :meth:`allocate` method of all children
        first, allowing the :meth:`_allocate` method to override the
        parameters of the children if needed.

        Raises
        ------
        ValueError
            If the configuration of this brick is insufficient to determine
            the number of parameters or their dimensionality to be
            initialized.

        Notes
        -----
        This method sets the :attr:`params` attribute to an empty list.
        This is in order to ensure that calls to this method completely
        reset the parameters.

        """
        if not self.allocation_config_pushed:
            self.push_allocation_config()
        for child in self.children:
            try:
                child.allocate()
            except:
                self.allocation_config_pushed = False
                raise
        self.params = []
        try:
            self._allocate()
        except Exception:
            if self.lazy:
                reraise_as("Lazy initialization is enabled, so please make "
                           "sure you have set all the required configuration "
                           "for this method call.")
            else:
                raise
        self.allocated = True

    def _allocate(self):
        """Brick implementation of parameter initialization.

        Implement this if your brick needs to allocate its parameters.

        .. warning::

           This method should never be called directly. Call
           :meth:`initialize` instead.

        """
        pass

    def initialize(self):
        """Initialize parameters.

        Intialize parameters, such as weight matrices and biases.

        Notes
        -----
        If the brick has not allocated its parameters yet, this method will
        call the :meth:`allocate` method in order to do so.
        """
        if not self.allocated:
            self.allocate()
        if not self.initialization_config_pushed:
            self.push_initialization_config()
        for child in self.children:
            try:
                child.initialize()
            except:
                self.initialization_config_pushed = False
                raise
        try:
            self._initialize()
        except Exception:
            if self.lazy:
                reraise_as("Lazy initialization is enabled, so please make "
                           "sure you have set all the required configuration "
                           "for this method call.")
            else:
                raise
        self.initialized = True

    def _initialize(self):
        """Brick implementation of parameter initialization.

        Implement this if your brick needs to initialize its parameters.

        .. warning::

           This method should never be called directly. Call
           :meth:`initialize` instead.

        """
        pass

    def push_allocation_config(self):
        """Push the configuration for allocation to child bricks.

        Bricks can configure their children, based on their own current
        configuration. This will be automatically done by a call to
        :meth:`allocate`, but if you want to override the configuration of
        child bricks manually, then you can call this function manually.

        """
        self._push_allocation_config()
        self.allocation_config_pushed = True

    def _push_allocation_config(self):
        """Brick implementation of configuring child before allocation.

        Implement this if your brick needs to set the configuration of its
        children before allocation.

        .. warning::

           This method should never be called directly. Call
           :meth:`push_allocation_config` instead.

        """
        pass

    def push_initialization_config(self):
        """Push the configuration for initialization to child bricks.

        Bricks can configure their children, based on their own current
        configuration. This will be automatically done by a call to
        :meth:`initialize`, but if you want to override the configuration
        of child bricks manually, then you can call this function manually.

        """
        self._push_initialization_config()
        self.initialization_config_pushed = True

    def _push_initialization_config(self):
        """Brick implementation of configuring child before initialization.

        Implement this if your brick needs to set the configuration of its
        children before initialization.

        .. warning::

           This method should never be called directly. Call
           :meth:`push_initialization_config` instead.

        """
        pass


class ApplySignature(object):
    """Base class for signatures of apply methods.

    A signature is a piece of information provided by an apply method about
    its inputs and outputs. Signatures are necessary for combining bricks
    into complex structures automatically. Inputs and outputs of an
    apply method can depend on the state of the brick, that's why the signature
    is returned by a special signature function, which is attached to
    an apply method by means of decorator at the class definition stage. In
    addition the returned value of the signature function can change during
    the brick's lifetime, e.g. after allocation of the brick the signature
    can contain dimensionalities.

    Attributes
    ----------
    output_names : list of strs
        The names for the output variables of the apply method
        given in the same order as they are returned. None if not available.
    output_dims : list of ints
        The expected dimensionalities of the output variables of the apply
        method given in the same order as they are returned.
        None if not available.

    .. todo:: support `OrderedDict` for the `output_dims` attribute

    """
    __metaclass__ = ABCMeta

    def __init__(self, output_names=None, output_dims=None):
        self.__dict__.update(**locals())
        del self.self


class DefaultRNG(Brick):
    """A mixin class for Bricks which need a RNG to initialize.

    Parameters
    ----------
    rng : object
        A ``numpy.RandomState`` instance.
    **kwargs
        Keyword arguments to pass on to the :meth:`Brick` base class.

    Attributes
    ----------
    rng : object
        If the RNG has been set, return it. Otherwise, return a RNG with a
        default seed which can be set at a module level using
        ``blocks.bricks.DEFAULT_SEED = seed``.

    """
    def __init__(self, rng=None, **kwargs):
        self.rng = rng
        super(DefaultRNG, self).__init__(**kwargs)

    @property
    def rng(self):
        if getattr(self, '_rng', None) is not None:
            return self._rng
        else:
            return np.random.RandomState(DEFAULT_SEED)

    @rng.setter
    def rng(self, rng):
        self._rng = rng


class Linear(DefaultRNG):
    """A linear transformation with optional bias.

    Linear brick which applies a linear (affine) transformation by
    multiplying the input with a weight matrix. Optionally a bias is added.

    Parameters
    ----------
    input_dim : int
        The dimension of the input. Required by :meth:`allocate`.
    output_dim : int
        The dimension of the output. Required by :meth:`allocate`.
    weights_init : object
        A `NdarrayInitialization` instance which will be used by to
        initialize the weight matrix. Required by :meth:`initialize`.
    biases_init : object, optional
        A `NdarrayInitialization` instance that will be used to initialize
        the biases. Required by :meth:`initialize` when `use_bias` is
        `True`.
    use_bias : bool, optional
        Whether to use a bias. Defaults to `True`. Required by
        :meth:`initialize`.

    Notes
    -----

    A linear transformation with bias is a matrix multiplication followed
    by a vector summation.

    .. math:: f(\mathbf{x}) = \mathbf{W}\mathbf{x} + \mathbf{b}

    """
    @Brick.lazy_method
    def __init__(self, input_dim, output_dim, weights_init,
                 biases_init=None, use_bias=True, **kwargs):
        self.__dict__.update(locals())
        del self.self
        super(Linear, self).__init__(**kwargs)

    def _allocate(self):
        self.params.append(shared_floatx_zeros((self.input_dim,
                                                self.output_dim)))
        if self.use_bias:
            self.params.append(shared_floatx_zeros((self.output_dim,)))

    def _initialize(self):
        if self.use_bias:
            W, b = self.params
            self.biases_init.initialize(b, self.rng)
        else:
            W, = self.params
        self.weights_init.initialize(W, self.rng)

    @Brick.apply_method
    def apply(self, inp):
        """Apply the linear transformation.

        Parameters
        ----------
        inp : Theano variable
            The input on which to apply the transformation

        Returns
        -------
        output : Theano variable
            The transformed input plus optional bias

        """
        if self.use_bias:
            W, b = self.params
        else:
            W, = self.params
        output = tensor.dot(inp, W)
        if self.use_bias:
            output += b
        return output


def _activation_factory(name, activation):
    """Class factory for Bricks which perform simple Theano calls."""
    class Activation(Brick):
        """Element-wise application of {0} function.

        Parameters
        ----------
        inp : Theano variable
            The Theano variable on which to apply the {0} function..

        Returns
        -------
        output : Theano variable
            The Theano variable with the {0} function applied.

        """
        @Brick.apply_method
        def apply(self, inp):
            """Apply the {0} function element-wise.

            Parameters
            ----------
            inp : Theano variable
                Theano variable to apply {0} to, element-wise.

            Returns
            -------
            output : Theano variable
                The input with the activation function applied.

            """
            output = activation(inp)
            return output
    Activation.__name__ = name
    Activation.__doc__ = Activation.__doc__.format(name.lower())
    Activation.apply.__doc__ = \
        Activation.apply.__doc__.format(name.lower())
    return Activation

Identity = _activation_factory('Identity', lambda x: x)
Tanh = _activation_factory('Tanh', tensor.tanh)
Sigmoid = _activation_factory('Sigmoid', tensor.nnet.sigmoid)
Softmax = _activation_factory('Softmax', tensor.nnet.softmax)


class MLP(DefaultRNG):
    """A simple multi-layer perceptron

    Parameters
    ----------
    activations : bricks or ``None``
        A list of activations to apply after each linear transformation.
        Give ``None`` to not apply any activation. Required for
        :meth:`__init__`.
    dims : list of ints
        A list of input dimensions, as well as the output dimension of the
        last layer. Required for :meth:`allocate`.
    weights_init : :class:`utils.NdarrayInitialization`
        The initialization scheme to initialize all the weights with.
    biases_init : :class:`utils.NdarrayInitialization`
        The initialization scheme to initialize all the biases with.
    use_bias : bool
        Whether or not to use biases.

    Notes
    -----
    Note that the ``weights_init``, ``biases_init`` and ``use_bias``
    configurations will overwrite those of the layers each time the
    :class:`MLP` is re-initialized. For more fine-grained control, push the
    configuration to the child layers manually before initialization.

    >>> from blocks.initialization import IsotropicGaussian, Constant
    >>> mlp = MLP([Tanh(), None], [30, 20, 10],
    ...           IsotropicGaussian(), Constant(0))
    >>> mlp.push_initialization_config()  # Configure children
    >>> mlp.children[0].weights_init = IsotropicGaussian(0.1)
    >>> mlp.initialize()

    """

    @Brick.lazy_method
    def __init__(self, activations, dims, weights_init, biases_init=None,
                 use_bias=True, **kwargs):
        super(MLP, self).__init__(**kwargs)
        self.linear_transformations = [Linear(name='linear_{}'.format(i))
                                       for i in range(len(activations))]
        self.children = (self.linear_transformations +
                         [activation for activation in activations
                          if activation is not None])
        if not dims:
            dims = [None] * (len(activations) + 1)
        self.__dict__.update(locals())
        del self.self

    def _push_allocation_config(self):
        assert len(self.dims) - 1 == len(self.linear_transformations)
        for input_dim, output_dim, layer in zip(self.dims[:-1], self.dims[1:],
                                                self.linear_transformations):
            layer.input_dim = input_dim
            layer.output_dim = output_dim

    def _push_initialization_config(self):
        for layer in self.linear_transformations:
            for attr in ['weights_init', 'use_bias', 'biases_init']:
                setattr(layer, attr, getattr(self, attr))

    @Brick.apply_method
    def apply(self, inp):
        output = inp
        for activation, linear in zip(self.activations,
                                      self.linear_transformations):
            if activation is None:
                output = linear.apply(output)
            else:
                output = activation.apply(linear.apply(output))
        return output


class Wrap3D(Brick):
    """Convert 3D arrays to 2D and back in order to apply 2D bricks."""
    @Brick.lazy_method
    def __init__(self, child, apply_method='apply', **kwargs):
        super(Wrap3D, self).__init__(**kwargs)
        self.children = [child]
        self.apply_method = apply_method

    @Brick.apply_method
    def apply(self, inp):
        child, = self.children
        flat_shape = ([inp.shape[0] * inp.shape[1]] +
                      [inp.shape[i] for i in range(2, inp.ndim)])
        output = getattr(child, self.apply_method)(inp.reshape(flat_shape))
        full_shape = ([inp.shape[0], inp.shape[1]] +
                      [output.shape[i] for i in range(1, output.ndim)])
        return output.reshape(full_shape)


class MultiInputApplySignature(ApplySignature):
    """Signature of an apply method with multiple inputs.

    Attributes:
    ----------
    forkable_input_names : list of strs
        Names of the inputs that can be forked from a common one.
    dims : dict
        A dictionary of available dimensionalities. Keys are names,
        value are ints.

    """

    def __init__(self, forkable_input_names=None, dims=None, **kwargs):
        super(MultiInputApplySignature, self).__init__(**kwargs)
        if not forkable_input_names:
            forkable_input_names = list()
        if not dims:
            dims = dict()
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs


class RecurrentApplySignature(MultiInputApplySignature):
    """Signature of a recurrent apply method.

    Attributes
    ----------
    input_names : list of strs
        Names of the arguments of the apply method that play input roles.
    state_names : list of strs
        Names of the arguments of the apply method that play state roles.
    context_names : list of strs
        Names of the arguments of the apply method that play context roles.
    num_outputs : int
        Number of outputs of the apply method.
    state_init_funcs : dict
        A dictionary for hidden states initialization functions. Keys are
        state names, values are functions.

    """

    def __init__(self, input_names=None, state_names=None, context_names=None,
                 num_outputs=0, **kwargs):
        super(RecurrentApplySignature, self).__init__(**kwargs)
        if not input_names:
            input_names = []
        if not state_names:
            state_names = []
        if not context_names:
            context_names = []
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs
        self.state_init_funcs = dict()

    def scan_arguments(self):
        return self.input_names + self.state_names + self.context_names


def zero_state(dim, batch_size, *args, **kwargs):
    """Create an initial state consisting of zeros.

    The default state initialization routine. It is not made a method
    to ensure that the brick argument can be omitted.

    """
    return tensor.zeros((batch_size, dim), dtype=theano.config.floatX)


class BaseRecurrent(Brick):
    """Base class for recurrent bricks.

    A recurrent network processes sequences by applying recursively
    a transition operator. This class contains some useful routine
    that fascilitate simple and boilerplate code free implementation
    of recurrent bricks.

    """

    @staticmethod
    def recurrent_apply_method(func):
        """Wraps an apply method to allow its recurrent application.

        This decorator allows you to use implementation
        of an RNN transition to process sequences without writing
        the iteration-related code again and again. In the most general
        form information flow of a recurrent network
        can be described as follows: depending on the context variables
        and driven by input sequences the RNN updates its states and
        produces output sequences. Thus the input variables of
        your transition function play one of three roles: an input,
        a context or a state. These roles should be specified in the
        method's signature to make iteration possible.

        """
        arg_spec = inspect.getargspec(func)
        arg_names = arg_spec.args[1:]

        def recurrent_apply(wrapper, brick, *args, **kwargs):
            """Iterates a transition function.

            Parameters
            ----------
            iterate : bool
                If ``True`` iteration is made. By default ``False``
                which means that transition function is applied as is.
            reverse : bool
                If ``True``, the inputs are processed in backward
                direction. ``False`` by default.

            .. todo::

                * Handle `updates` returned by the `theano.scan`
                    routine.
                * ``kwargs`` has a random order; check if this is a
                    problem.

            """
            # Extract arguments related to iteration.
            iterate = kwargs.pop("iterate", False)
            if not iterate:
                return func(brick, *args, **kwargs)
            reverse = kwargs.pop("reverse", False)
            return_initial_states = kwargs.pop("return_initial_states", False)

            signature = wrapper.signature(brick, *args, **kwargs)
            assert isinstance(signature, RecurrentApplySignature)

            # Push everything to kwargs
            for arg, arg_name in zip(args, arg_names):
                kwargs[arg_name] = arg
            # Separate kwargs that aren't input, context or state variables
            rest_kwargs = {key: value for key, value in kwargs.items()
                           if key not in signature.scan_arguments()}

            # Check what is given and what is not
            def only_given(arg_names):
                return OrderedDict((arg_name, kwargs[arg_name])
                                   for arg_name in arg_names
                                   if arg_name in kwargs)
            inputs_given = only_given(signature.input_names)
            contexts_given = only_given(signature.context_names)

            # TODO Assumes 1 time dim!
            if len(inputs_given):
                shape = inputs_given.values()[0].shape
                n_steps = shape[0]
                batch_size = shape[1]
            else:
                n_steps = kwargs.pop('n_steps')
                batch_size = kwargs.pop('batch_size')

            # Ensure that all initial states are available.
            for state_name in signature.state_names:
                if not kwargs.get(state_name):
                    dim = signature.dims[state_name]
                    init_func = signature.state_init_funcs.get(
                        state_name, zero_state)
                    kwargs[state_name] = init_func(dim, batch_size, brick,
                                                   *args, **kwargs)
            states_given = only_given(signature.state_names)
            # Theano issue 1772
            for name, state in states_given.items():
                if state.ndim == 2:
                    states_given[name] = tensor.unbroadcast(state, 1)
            assert len(states_given) == len(signature.state_names)

            def scan_function(*args):
                args = list(args)
                arg_names = (inputs_given.keys() + states_given.keys() +
                             contexts_given.keys())
                kwargs = dict(zip(arg_names, args))
                kwargs.update(rest_kwargs)
                return func(brick, **kwargs)
            result, updates = theano.scan(
                scan_function, sequences=inputs_given.values(),
                outputs_info=(states_given.values()
                              + [None] * signature.num_outputs),
                non_sequences=contexts_given.values(),
                n_steps=n_steps,
                go_backwards=reverse)
            if not isinstance(result, list):
                result = [result]
            if return_initial_states:
                # Undo the subtensor
                for i in range(len(states_given)):
                    result[i] = result[i].owner.inputs[0]
            assert not updates  # TODO Handle updates
            return result

        return Brick._apply_method(True)(recurrent_apply)


class ForkInputs(Brick):
    """Wraps a block with multiple inputs to use one common input.

    Parameters
    ----------
    child : Brick
        The brick with multiple inputs to wrap.
    input_dim : int
        The common input dimensionality.
    apply_method : str
        The name of the wrapped brick apply method to use.
    fork : Brick
        A prototype for fork bricks.
    weights_init : `utils.NdarrayInitialization`
        Weight initilization method to propagate to fork bricks.
    biases_init : `utils.NdarrayInitialization`
        Biases initialization method to propagate to fork bricks.

    """
    common_input = 'common_input'

    @Brick.lazy_method
    def __init__(self, child, input_dim, apply_method='apply',
                 fork=None, weights_init=None, biases_init=None, **kwargs):
        super(ForkInputs, self).__init__(**kwargs)
        self.__dict__.update(**locals())
        del self.self
        del self.kwargs

        self.wrapped_apply = getattr(child, self.apply_method)

        signature = self.child.signature('apply')
        assert isinstance(signature, MultiInputApplySignature)
        if not fork:
            fork = MLP([Identity()])
        self.forks = []
        for input_name in signature.forkable_input_names:
            fork_copy = copy.deepcopy(fork)
            fork_copy.name = "{}_{}".format("fork", input_name)
            self.forks.append(fork_copy)
        self.children = [child] + self.forks

    def _push_allocation_config(self):
        signature = self.child.signature('apply')
        for name, fork in zip(signature.forkable_input_names, self.forks):
            fork.dims[0] = self.input_dim
            fork.dims[-1] = signature.dims[name]

    def _push_initialization_config(self):
        for fork in self.forks:
            if self.weights_init:
                fork.weights_init = self.weights_init
            if self.biases_init:
                fork.biases_init = self.biases_init

    @BaseRecurrent.apply_method
    def apply(self, common_input, **kwargs):
        """Apply the child brick with forked inputs.

        Parameters
        ----------
        common_input : Theano variable
            The input to fork.

        """
        signature = self.child.signature('apply')
        for name, fork in zip(signature.forkable_input_names, self.forks):
            assert name not in kwargs
            kwargs[name] = fork.apply(common_input)
        return self.wrapped_apply(**kwargs)

    @apply.signature_method
    def apply_signature(self):
        signature = copy.deepcopy(self.child.signature('apply'))
        if isinstance(signature, RecurrentApplySignature):
            signature.input_names = (
                [name for name in signature.input_names
                 if name not in signature.forkable_input_names]
                + ["common_input"])
        signature.forkable_input_names = "common_input"
        return signature


class Recurrent(BaseRecurrent, DefaultRNG):
    """Simple recurrent layer with optional activation.

    Parameters
    ----------
    dim : int
        The dimension of the hidden state
    weights_init : object
        The :class:`utils.NdarrayInitialization` object to initialize the
        weight matrix with.
    activation : Brick
        The brick to apply as activation.

    .. todo::

       Implement deep transitions (by using other bricks). Currently, this
       probably re-implements too much from the Linear brick.

       Other important features:

       * Carrying over hidden state between batches
       * Return k last hidden states

    """
    @Brick.lazy_method
    def __init__(self, dim, weights_init, activation=None, **kwargs):
        super(Recurrent, self).__init__(**kwargs)
        if not activation:
            activation = Identity()
        self.__dict__.update(locals())
        del self.self

    @property
    def W(self):
        return self.params[0]

    def _allocate(self):
        self.params.append(shared_floatx_zeros((self.dim, self.dim)))

    def _initialize(self):
        self.weights_init.initialize(self.W, self.rng)

    @BaseRecurrent.recurrent_apply_method
    def apply(self, inp, state, mask=None):
        """Given data and mask, apply recurrent layer.

        Parameters
        ----------
        inp : Theano variable
            The 2 dimensional input, in the shape (batch, features).
        state : Theano variable
            The 2 dimensional state, in the shape (batch, features).
        mask : Theano variable
            A 1D binary array in the shape (batch,) which is 1 if
            there is data available, 0 if not. Assumed to be 1-s
            only if not given.

        .. todo::

           * Mask should become part of ``MaskedTensorVariable`` type so
             that it can be passed around transparently.
           * We should stop assuming that batches are the second dimension,
             in order to support nested RNNs i.e. where the first n axes
             are time, n + 1 is the batch, and n + 2, ... are features.
             Masks will become n + 1 dimensional as well then.

        """
        dtype = self.W.dtype
        check_theano_variable(inp, 2, dtype)
        check_theano_variable(state, 2, dtype)
        check_theano_variable(mask, 1, dtype)

        next_state = inp + tensor.dot(state, self.W)
        next_state = self.activation.apply(next_state)
        if mask:
            next_state = (mask[:, None] * next_state +
                          (1 - mask[:, None]) * state)
        return next_state

    @apply.signature_method
    def apply_signature(self, *args, **kwargs):
        return RecurrentApplySignature(
            input_names=['inp', 'mask'], forkable_input_names=['inp'],
            state_names=['state'], output_names=['state'],
            dims=dict(state=self.dim))


class GatedRecurrent(BaseRecurrent, DefaultRNG):
    """Gated recurrent neural network.

    Gated recurrent neural network (GRNN) as introduced in [1]. Every unit
    of a GRNN is equiped with update and reset gates that fascilitate better
    gradient propagation.

    Parameters
    ----------
    activation : Brick or None
        The brick to apply as activation. If `None` an `Identity` brick is
        used.
    gated_activation : Brick or None
        The brick to apply as activation for gates. If `None` a `Sigmoid`
        brick is used.
    dim : int
        The dimension of the hidden state.
    weights_init : object
        The :class:`utils.NdarrayInitialization` object to initialize the
        weight matrix with.
    use_upgate_gate : bool
        If True the update gates are used.
    use_reset_gate : bool
        If True the reset gates are used.


    .. [1] Kyunghyun Cho, Bart van Merrienboer, Caglar Gulcehre,
         Dzmitry Bahdanau, Fethi Bougares, Holger Schwenk and Yoshua Bengio.
         Learning Phrase Representations using RNN Encoder-Decoder
         for Statistical Machine Translation. EMNLP 2014.
    """

    @Brick.lazy_method
    def __init__(self, activation, gate_activation, dim, weights_init,
                 use_update_gate=True, use_reset_gate=True, **kwargs):
        super(BaseRecurrent, self).__init__(**kwargs)

        if not activation:
            activation = Identity()
        if not gate_activation:
            gate_activation = Sigmoid()

        self.__dict__.update(locals())
        del self.self
        del self.kwargs

    @property
    def state2state(self):
        return self.params[0]

    @property
    def state2update(self):
        return self.params[1]

    @property
    def state2reset(self):
        return self.params[2]

    def _allocate(self):
        def new_param(name):
            shared = shared_floatx_zeros((self.dim, self.dim))
            shared.name = name
            return shared
        self.params.append(new_param('state2state'))
        self.params.append(new_param('state2update')
                           if self.use_update_gate else None)
        self.params.append(new_param('state2reset')
                           if self.use_reset_gate else None)

    def _initialize(self):
        self.weights_init.initialize(self.state2state, self.rng)
        if self.use_update_gate:
            self.weights_init.initialize(self.state2update, self.rng)
        if self.use_reset_gate:
            self.weights_init.initialize(self.state2reset, self.rng)

    @BaseRecurrent.recurrent_apply_method
    def apply(self, inps, update_inps=None, reset_inps=None,
              states=None, mask=None):
        """Apply the gated recurrent transition.

        Parameters
        ----------
        states : Theano variable
            The 2 dimensional matrix of current states in the shape
            (batch_size, features). Required for `one_step` usage.
        inps : Theano matrix of floats
            The 2 dimensional matrix of inputs in the shape
            (batch_size, features)
        update_inps : Theano variable
            The 2 dimensional matrix of inputs to the update gates in the shape
            (batch_size, features). None when the update gates are not used.
        reset_inps : Theano variable
            The 2 dimensional matrix of inputs to the reset gates in the shape
            (batch_size, features). None when the reset gates are not used.
        mask : Theano variable
            A 1D binary array in the shape (batch,) which is 1 if
            there is data available, 0 if not. Assumed to be 1-s
            only if not given.

        Returns
        -------
        output : Theano variable
            Next states of the network.
        """
        if (self.use_update_gate != bool(update_inps) or
                self.use_reset_gate != bool(reset_inps)):
            raise ValueError("Configuration and input mismatch:"
                             "\n\tyou should provide inputs for gates if"
                             " and only if the gates are on.")

        dtype = self.state2state.dtype
        for inp in [states, inps, update_inps, reset_inps]:
            check_theano_variable(inp, 2, dtype)
        check_theano_variable(mask, 1, dtype)

        states_reseted = states

        if self.use_reset_gate:
            reset_values = self.gate_activation.apply(
                states.dot(self.state2reset) + reset_inps)
            states_reseted = states * reset_values

        next_states = self.activation.apply(
            states_reseted.dot(self.state2state) + inps)

        if self.use_update_gate:
            update_values = self.gate_activation.apply(
                states.dot(self.state2update) + update_inps)
            next_states = (next_states * update_values
                           + states * (1 - update_values))

        if mask:
            next_states = (mask[:, None] * next_states
                           + (1 - mask[:, None]) * states)

        return next_states

    @apply.signature_method
    def apply_signature(self, *args, **kwargs):
        s = RecurrentApplySignature(
            input_names=['mask', 'inps'], state_names=['states'],
            output_names=['states'],
            dims=dict(inps=self.dim, states=self.dim))
        if self.use_update_gate:
            s.input_names.append('update_inps')
            s.dims['update_inps'] = self.dim
        if self.use_reset_gate:
            s.input_names.append('reset_inps')
            s.dims['reset_inps'] = self.dim
        s.forkable_input_names = s.input_names[1:]
        return s


class BidirectionalRecurrent(DefaultRNG):
    @Brick.lazy_method
    def __init__(self, dim, weights_init, activation=None, hidden_init=None,
                 combine='concatenate', **kwargs):
        super(BidirectionalRecurrent, self).__init__(**kwargs)
        if hidden_init is None:
            hidden_init = Constant(0)
        self.__dict__.update(locals())
        del self.self
        self.children = [Recurrent(), Recurrent()]

    def _push_allocation_config(self):
        for child in self.children:
            for attr in ['dim', 'activation', 'hidden_init']:
                setattr(child, attr, getattr(self, attr))

    def _push_initialization_config(self):
        for child in self.children:
            child.weights_init = self.weights_init

    @Brick.apply_method
    def apply(self, inp, mask):
        forward = self.children[0].apply(inp, mask)
        backward = self.children[1].apply(inp, mask, reverse=True)
        output = tensor.concatenate([forward[-1], backward[-1]], axis=1)
        return output
