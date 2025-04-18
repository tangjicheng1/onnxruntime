# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import contextlib
import copy
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
import onnx

import onnxruntime.training.onnxblock._graph_utils as _graph_utils
import onnxruntime.training.onnxblock.model_accessor as accessor


class Block(ABC):
    """Base class for all building blocks that can be stacked on top of each other.

    All blocks that want to manipulate the model must subclass this class. The subclass's
    implementation of the build method must return the names of the intermediate outputs from
    the block.

    The subclass's implementation of the build method must manipulate the base model as it deems fit,
    but the manipulated model must be valid (as deemed by the onnx checker).

    Attributes:
        base (onnx.ModelProto): The base model that the subclass can manipulate.
    """

    def __init__(self, temp_file_name="temp.onnx"):
        if os.path.isabs(temp_file_name):
            raise RuntimeError("Please pass in a relative path for the temp_file_name.")
        self.base = None
        self.temp_onnx_file_path = os.path.join(os.getcwd(), temp_file_name)
        # onnx.save location parameter requires a relative path to the model path
        self.temp_external_data_file_name = temp_file_name + ".data"

    @abstractmethod
    def build(self, *args, **kwargs):
        """Customize the model by stacking up blocks on top of the inputs to this function.

        This method must be overridden by the subclass.
        """

    def __call__(self, *args, **kwargs):
        """Calls the subclass's build method and runs validation on top."""

        self.base = accessor._GLOBAL_ACCESSOR.model

        logging.debug("Building block: %s", self.__class__.__name__)

        output = self.build(*args, **kwargs)

        if accessor._GLOBAL_ACCESSOR.has_path:
            # `save` will destructively access any external data
            copied_model = copy.deepcopy(accessor._GLOBAL_ACCESSOR.model)
            onnx.save(
                copied_model,
                self.temp_onnx_file_path,
                save_as_external_data=True,
                all_tensors_to_one_file=True,
                location=self.temp_external_data_file_name,
            )

            onnx.checker.check_model(self.temp_onnx_file_path, True)
        else:
            onnx.checker.check_model(self.base, True)

        return output

    def infer_shapes_on_base(self):
        """
        Performs shape inference on the global model. If a path was used, then uses the
        infer_shapes_path API to support models with external data.

        Returns the shape-inferenced ModelProto.
        """
        if accessor._GLOBAL_ACCESSOR.has_path:
            onnx.save(
                accessor._GLOBAL_ACCESSOR.model,
                self.temp_onnx_file_path,
                save_as_external_data=True,
                all_tensors_to_one_file=True,
                location=self.temp_external_data_file_name,
            )

            onnx.shape_inference.infer_shapes_path(self.temp_onnx_file_path)
            # shape inferenced model is saved to original path
            model = onnx.load(self.temp_onnx_file_path)

            return model
        else:
            return onnx.shape_inference.infer_shapes(accessor._GLOBAL_ACCESSOR.model)

    def __del__(self):
        # since the ModelProto does not store the external data parameters themselves, just the metadata
        # for where the external data can be found, we retain the external data files for the intermediate
        # calls until the Block no longer needs to be used.
        if os.path.exists(self.temp_onnx_file_path):
            os.remove(self.temp_onnx_file_path)
            # get absolute path for the external data file
            external_data_file_path = os.path.join(
                os.path.dirname(self.temp_onnx_file_path), self.temp_external_data_file_name
            )
            if os.path.exists(external_data_file_path):
                os.remove(external_data_file_path)


class _BinaryOp(Block):
    def __init__(self, op_name):
        super().__init__()
        self._op_name = op_name

    def build(self, input_name1, input_name2):
        # get the model to manipulate
        onnx_model = self.base

        # Assert that the op name is not empty
        if not self._op_name:
            raise RuntimeError("Unknown op name. Please override _op_name")

        # create the graph node for sub
        node_input_names = [input_name1, input_name2]
        node_output_name = _graph_utils.generate_graph_name(f"{self._op_name.lower()}_output")
        node_output_names = [node_output_name]
        node = onnx.helper.make_node(
            self._op_name,
            node_input_names,
            node_output_names,
            name=_graph_utils.generate_graph_name(self._op_name),
        )
        onnx_model.graph.node.append(node)

        return node_output_name


class Add(_BinaryOp):
    """Adds Add node to an onnx model."""

    def __init__(self):
        super().__init__("Add")


class Sub(_BinaryOp):
    """Adds Sub node to an onnx model."""

    def __init__(self):
        super().__init__("Sub")


class Mul(_BinaryOp):
    """Adds Mul node to an onnx model."""

    def __init__(self):
        super().__init__("Mul")


class Div(_BinaryOp):
    """Adds Div node to an onnx model."""

    def __init__(self):
        super().__init__("Div")


class Pow(Block):
    """Adds Pow node to the onnx model."""

    def __init__(self, exponent):
        super().__init__()

        self._exponent = exponent

    def build(self, pow_input_name):
        # get the model to manipulate
        onnx_model = self.base

        # create the graph initializer for the exponent
        pow_node_exponent_name = _graph_utils.generate_graph_name("pow_exponent")
        onnx_model.graph.initializer.append(
            onnx.helper.make_tensor(pow_node_exponent_name, onnx.TensorProto.FLOAT, [1], [self._exponent])
        )

        # create the graph node for pow
        pow_node_input_names = [pow_input_name, pow_node_exponent_name]
        pow_node_output_name = _graph_utils.generate_graph_name("pow_output")
        pow_node_output_names = [pow_node_output_name]
        pow_node = onnx.helper.make_node(
            "Pow",
            pow_node_input_names,
            pow_node_output_names,
            name=_graph_utils.generate_graph_name("Pow"),
        )
        onnx_model.graph.node.append(pow_node)

        return pow_node_output_name


class _UnaryOp(Block):
    """Base class for all nodes that take in a single argument."""

    def __init__(self, op_name, **attributes):
        super().__init__()
        self._op_name = op_name
        self._attributes = attributes

    def build(self, input_name):
        # get the model to manipulate
        onnx_model = self.base

        # Assert that the op name is not empty
        if not self._op_name:
            raise RuntimeError("Unknown op name. Please override _op_name")

        # create the graph node for this unary op
        node_input_names = [input_name]
        node_output_name = _graph_utils.generate_graph_name(f"{self._op_name.lower()}_output")
        node_output_names = [node_output_name]
        node = onnx.helper.make_node(
            self._op_name,
            node_input_names,
            node_output_names,
            _graph_utils.generate_graph_name(self._op_name),
            **self._attributes,
        )
        onnx_model.graph.node.append(node)

        return node_output_name


class ReduceMean(_UnaryOp):
    """Adds ReduceMean node to the onnx model."""

    def __init__(self, keepdims=True):
        super().__init__("ReduceMean", keepdims=keepdims)


class ReduceSum(_UnaryOp):
    """Adds ReduceSum node to the onnx model."""

    def __init__(self, keepdims=True):
        super().__init__("ReduceSum", keepdims=keepdims)


class Sigmoid(_UnaryOp):
    """Adds Sigmoid node to the onnx model."""

    def __init__(self):
        super().__init__("Sigmoid")


class Log(_UnaryOp):
    """Adds Log node to the onnx model."""

    def __init__(self):
        super().__init__("Log")


class Neg(_UnaryOp):
    """Adds Neg node to the onnx model."""

    def __init__(self):
        super().__init__("Neg")


class Abs(_UnaryOp):
    """Adds Abs node to the onnx model."""

    def __init__(self):
        super().__init__("Abs")


class Constant(Block):
    """Creates a float initializer and adds it to the onnx model."""

    # TODO: Add ability to add all sorts of initializers (not just floats).

    def __init__(self, value_float):
        super().__init__()

        self._value = value_float

    def build(self):
        # create the graph initializer for the exponent
        initializer_name = _graph_utils.generate_graph_name("initializer")
        self.base.graph.initializer.append(
            onnx.helper.make_tensor(initializer_name, onnx.TensorProto.FLOAT, [1], [self._value])
        )
        return initializer_name


class SequenceConstruct(Block):
    """Adds SequenceConstruct node to the onnx model."""

    def build(self, *sequence_input_names):
        sc_node_input_names = list(sequence_input_names)
        sc_node_output_name = _graph_utils.generate_graph_name("sequenceconstruct_output")
        sc_node_output_names = [sc_node_output_name]
        sc_node = onnx.helper.make_node(
            "SequenceConstruct",
            sc_node_input_names,
            sc_node_output_names,
            _graph_utils.generate_graph_name("SequenceConstruct"),
        )
        self.base.graph.node.append(sc_node)

        return sc_node_output_name


class ReduceAllL2(Block):
    """Adds ReduceAllL2 node to the onnx model.

    ReduceAllL2 is a part of the com.microsoft domain and might not be accessible outside this domain.
    """

    def build(self, *reduce_node_input_names):
        reduce_node_input_names = list(reduce_node_input_names)
        reduce_node_output_name = _graph_utils.generate_graph_name("reducealll2_output")
        reduce_node_output_names = [reduce_node_output_name]
        reduce_node = onnx.helper.make_node(
            "ReduceAllL2",
            reduce_node_input_names,
            reduce_node_output_names,
            _graph_utils.generate_graph_name("ReduceAllL2"),
            domain="com.microsoft",
        )
        self.base.graph.node.append(reduce_node)
        # TODO: register shape inference with onnx
        self.base.graph.value_info.append(
            onnx.helper.make_tensor_value_info(reduce_node_output_name, onnx.TensorProto.FLOAT, [1])
        )

        return reduce_node_output_name


class Clip(Block):
    """Adds Clip node to the onnx model."""

    def __init__(self, clip_min=None, clip_max=None):
        super().__init__()

        self._min = clip_min
        self._max = clip_max

    def build(self, clip_input_name):
        # get the model to manipulate
        onnx_model = self.base

        # create the graph initializer for the clip min
        clip_node_min_name = ""
        if self._min is not None:
            clip_node_min_name = _graph_utils.generate_graph_name("clip_min")
            onnx_model.graph.initializer.append(
                onnx.helper.make_tensor(clip_node_min_name, onnx.TensorProto.FLOAT, [1], [self._min])
            )

        # create the graph initializer for the clip max
        clip_node_max_name = ""
        if self._max is not None:
            clip_node_max_name = _graph_utils.generate_graph_name("clip_max")
            onnx_model.graph.initializer.append(
                onnx.helper.make_tensor(clip_node_max_name, onnx.TensorProto.FLOAT, [1], [self._max])
            )

        # create the graph node for this clip node
        clip_node_input_names = [
            clip_input_name,
            clip_node_min_name,
            clip_node_max_name,
        ]
        clip_node_output_name = _graph_utils.generate_graph_name("clip_output")
        clip_node_output_names = [clip_node_output_name]
        clip_node = onnx.helper.make_node(
            "Clip",
            clip_node_input_names,
            clip_node_output_names,
            _graph_utils.generate_graph_name("Clip"),
        )
        onnx_model.graph.node.append(clip_node)

        return clip_node_output_name


class PassThrough(Block):
    """A pass through block that returns the inputs without making any changes to onnx."""

    def build(self, *input_names):
        return input_names


class InputLike(Block):
    """Add an input to the onnx model like the graph input/output associated with the given name"""

    def __init__(self, like: str):
        """Create an input like the graph input/output associated with the given name

        Args:
            like (str): The name of the graph input/output to clone the type and shape from
        """
        super().__init__()

        self._like = like

    def build(self, input_name: str | None = None):
        cloned_input = None
        with contextlib.suppress(LookupError):
            # Suppress LookupError because we want to try to get the input from the output if it's not found in the inputs
            cloned_input = copy.deepcopy(_graph_utils.get_input_from_input_name(self.base, self._like))

        if cloned_input is None:
            with contextlib.suppress(LookupError):
                # Suppress LookupError because we deal with the case where no input or output was found later.
                cloned_input = copy.deepcopy(_graph_utils.get_output_from_output_name(self.base, self._like))

        if cloned_input is None:
            raise LookupError(f"Could not find input or output with name {self._like}")

        cloned_input.name = input_name or _graph_utils.generate_graph_name("input")
        self.base.graph.input.append(cloned_input)

        return cloned_input.name


class LabelEncoder(Block):
    def __init__(
        self,
        default_float: float = 0.0,
        default_int64: int = -1,
        default_string: str = "_Unused",
        keys_floats: list[float] | None = None,
        keys_int64s: list[int] | None = None,
        keys_strings: list[str] | None = None,
        values_floats: list[float] | None = None,
        values_int64s: list[int] | None = None,
        values_strings: list[str] | None = None,
    ):
        super().__init__()

        self._attributes = {
            "default_float": default_float,
            "default_int64": default_int64,
            "default_string": default_string,
        }

        def _add_attributes(names: list[str], values: list[Any]):
            for name, value in zip(names, values, strict=False):
                if value is not None:
                    self._attributes[name] = value

        _add_attributes(
            ["keys_floats", "keys_int64s", "keys_strings", "values_floats", "values_int64s", "values_strings"],
            [keys_floats, keys_int64s, keys_strings, values_floats, values_int64s, values_strings],
        )

    def build(self, label_encoder_input_name: str):
        label_encoder_output_name = _graph_utils.generate_graph_name("label_encoder.output")
        label_encoder_node = onnx.helper.make_node(
            "LabelEncoder",
            [label_encoder_input_name],
            [label_encoder_output_name],
            _graph_utils.generate_graph_name("LabelEncoder"),
            domain="ai.onnx.ml",
            **self._attributes,
        )
        self.base.graph.node.append(label_encoder_node)

        return label_encoder_output_name


class Cast(Block):
    def __init__(self, to: onnx.TensorProto.DataType):
        super().__init__()

        self._to = to

    def build(self, cast_input_name: str):
        cast_output_name = _graph_utils.generate_graph_name("cast.output")
        cast_node = onnx.helper.make_node(
            "Cast",
            [cast_input_name],
            [cast_output_name],
            _graph_utils.generate_graph_name("Cast"),
            to=self._to,
        )
        self.base.graph.node.append(cast_node)

        return cast_output_name


class Linear(Block):
    def __init__(self, in_features, out_features, bias=True, alpha=1.0, beta=1.0):
        super().__init__()

        self._in_features = in_features
        self._bias = bias
        self._out_features = out_features
        self._alpha = alpha
        self._beta = beta

    def build(self, linear_input_name: str):
        # Weight initializer
        linear_node_weight_name = _graph_utils.generate_graph_name("linear.weight")

        self.base.graph.initializer.append(
            onnx.numpy_helper.from_array(
                np.random.randn(self._in_features, self._out_features).astype(np.float32), linear_node_weight_name
            )
        )

        linear_node_input_names = [linear_input_name, linear_node_weight_name]

        # Bias initializer
        if self._bias:
            linear_node_bias_name = _graph_utils.generate_graph_name("linear.bias")
            self.base.graph.initializer.append(
                onnx.numpy_helper.from_array(
                    np.random.randn(self._out_features).astype(np.float32), linear_node_bias_name
                )
            )
            linear_node_input_names.append(linear_node_bias_name)

        linear_node_output_name = _graph_utils.generate_graph_name("linear.output")
        linear_node_output_names = [linear_node_output_name]
        linear_node = onnx.helper.make_node(
            "Gemm",
            linear_node_input_names,
            linear_node_output_names,
            _graph_utils.generate_graph_name("linear"),
            alpha=self._alpha,
            beta=self._beta,
        )

        self.base.graph.node.append(linear_node)

        return linear_node_output_name
