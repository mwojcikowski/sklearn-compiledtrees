from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals
from __future__ import print_function

from distutils import sysconfig

import contextlib
import os
import subprocess
import tempfile
from joblib import Parallel, delayed

import platform

if platform.system() == 'Windows':
    CXX_COMPILER = os.environ['CXX'] if 'CXX' in os.environ else None
    delete_files = False
else:
    CXX_COMPILER = sysconfig.get_config_var('CXX')
    delete_files = True

# detect OpenMP support
if platform.system() == 'Darwin':
    c_ver = subprocess.check_output([CXX_COMPILER, '--version']).decode('ascii')
    if c_ver.find('clang') >= 0:  # Xcode clang does not support OpenMP
        OPENMP_SUPPORT = False
    else:  # GCC supports OpenMP
        OPENMP_SUPPORT = True
else:
    OPENMP_SUPPORT = True

EVALUATE_FN_NAME = "evaluate"
ALWAYS_INLINE = "__attribute__((__always_inline__))"


class CodeGenerator(object):
    def __init__(self):
        self._file = tempfile.NamedTemporaryFile(mode='w+b',
                                                 prefix='compiledtrees_',
                                                 suffix='.cpp',
                                                 delete=delete_files)
        self._indent = 0

    @property
    def file(self):
        self._file.flush()
        return self._file

    def write(self, line):
        self._file.write(("  " * self._indent + line + "\n").encode("ascii"))

    @contextlib.contextmanager
    def bracketed(self, preamble, postamble):
        assert self._indent >= 0
        self.write(preamble)
        self._indent += 1
        yield
        self._indent -= 1
        self.write(postamble)


def code_gen_tree(tree, evaluate_fn=EVALUATE_FN_NAME, gen=None):
    """
    Generates C code representing the evaluation of a tree.

    Writes code similar to:
    ```
        extern "C" {
          __attribute__((__always_inline__)) double evaluate(float* f) {
            if (f[9] <= 0.175931170583) {
              return 0.0;
            }
            else {
              return 1.0;
            }
          }
        }
    ```

    to the given CodeGenerator object.
    """
    if gen is None:
        gen = CodeGenerator()

    def recur(node):
        if tree.children_left[node] == -1:
            assert tree.value[node].size == 1
            gen.write("return {0};".format(tree.value[node].item()))
            return

        branch = "if (f[{feature}] <= {threshold}f) {{".format(
            feature=tree.feature[node],
            threshold=tree.threshold[node])
        with gen.bracketed(branch, "}"):
            recur(tree.children_left[node])

        with gen.bracketed("else {", "}"):
            recur(tree.children_right[node])

    with gen.bracketed('extern "C" {', "}"):
        fn_decl = "{inline} double {name}(float* f) {{".format(
            inline=ALWAYS_INLINE,
            name=evaluate_fn)
        with gen.bracketed(fn_decl, "}"):
            recur(0)
    return gen.file


def _gen_tree(i, tree):
    """
    Generates cpp code for i'th tree.
    Moved out of code_gen_ensemble scope for parallelization.
    """
    name = "{name}_{index}".format(name=EVALUATE_FN_NAME, index=i)
    gen_tree = CodeGenerator()
    return code_gen_tree(tree, name, gen_tree)


def code_gen_ensemble(trees, individual_learner_weight, initial_value,
                      gen=None, n_jobs=1):
    """
    Writes code similar to:

    ```
    extern "C" {
      __attribute__((__always_inline__)) double evaluate_partial_0(float* f) {
        if (f[4] <= 0.662200987339) {
          return 1.0;
        }
        else {
          if (f[8] <= 0.804652512074) {
            return 0.0;
          }
          else {
            return 1.0;
          }
        }
      }
    }
    extern "C" {
      __attribute__((__always_inline__)) double evaluate_partial_1(float* f) {
        if (f[4] <= 0.694428026676) {
          return 1.0;
        }
        else {
          if (f[7] <= 0.4402526021) {
            return 1.0;
          }
          else {
            return 0.0;
          }
        }
      }
    }

    extern "C" {
      double evaluate(float* f) {
        double result = 0.0;
        result += evaluate_partial_0(f) * 0.1;
        result += evaluate_partial_1(f) * 0.1;
        return result;
      }
    }
    ```

    to the given CodeGenerator object.
    """

    if gen is None:
        gen = CodeGenerator()

    tree_files = [_gen_tree(i, tree) for i, tree in enumerate(trees)]
    if OPENMP_SUPPORT:
        gen.write("#include <omp.h>")

    with gen.bracketed('extern "C" {', "}"):
        # add dummy definitions if you will compile in parallel
        for i, tree in enumerate(trees):
            name = "{name}_{index}".format(name=EVALUATE_FN_NAME, index=i)
            gen.write("double {name}(float* f);".format(name=name))
        func_pointers = ", ".join(["&{name}_{index}".format(name=EVALUATE_FN_NAME, index=i) for i in range(len(trees))])
        gen.write("double (* funcs [{n}])(float* f) = {{{f}}};".format(f=func_pointers, n=len(trees)))
        fn_decl = "double {name}(float* f, int n_jobs) {{".format(name=EVALUATE_FN_NAME)
        with gen.bracketed(fn_decl, "}"):
            # if OPENMP_SUPPORT:
            #     gen.write("omp_set_num_threads(n_jobs);")
            gen.write("double result = {0};".format(initial_value))
            gen.write("int i;")
            gen.write("#pragma omp parallel for num_threads(n_jobs) schedule(static) private(i) reduction(+:result)")
            with gen.bracketed("for(int i=0; i<{n}; ++i)\n{{".format(n=len(trees)), "}"):
                increment = "result += funcs[i](f) * {weight};".format(
                    name=EVALUATE_FN_NAME,
                    index=i,
                    weight=individual_learner_weight)
                gen.write(increment)
            gen.write("return result;")
    return tree_files + [gen.file]


def _compile(cpp_f):
    if CXX_COMPILER is None:
        raise Exception("CXX compiler was not found. You should set CXX "
                        "environmental variable")
    o_f = tempfile.NamedTemporaryFile(mode='w+b',
                                      prefix='compiledtrees_',
                                      suffix='.o',
                                      delete=delete_files)
    if platform.system() == 'Windows':
        o_f.close()
    _call([CXX_COMPILER, cpp_f, "-c", "-fPIC", "-fopenmp" if OPENMP_SUPPORT else "", "-o", o_f.name, "-O3", "-pipe"])
    return o_f


def _call(args):
    DEVNULL = open(os.devnull, 'w')
    subprocess.check_call(" ".join(args),
                          shell=True, stdout=DEVNULL, stderr=DEVNULL)


def compile_code_to_object(files, n_jobs=1):
    # if ther is a single file then create single element list
    # unicode for filename; name attribute for file-like objects
    if isinstance(files, str) or hasattr(files, 'name'):
        files = [files]

    # Close files on Windows to avoid permission errors
    if platform.system() == 'Windows':
        for f in files:
            f.close()

    o_files = (Parallel(n_jobs=n_jobs, backend='threading')
               (delayed(_compile)(f.name) for f in files))

    so_f = tempfile.NamedTemporaryFile(mode='w+b',
                                       prefix='compiledtrees_',
                                       suffix='.so',
                                       delete=delete_files)
    # Close files on Windows to avoid permission errors
    if platform.system() == 'Windows':
        so_f.close()

    # link trees
    if platform.system() == 'Windows':
        # a hack to overcome large RFs on windows and CMD 9182 chaacters limit
        list_ofiles = tempfile.NamedTemporaryFile(mode='w+b',
                                                  prefix='list_ofiles_',
                                                  delete=delete_files)
        for f in o_files:
            list_ofiles.write((f.name.replace('\\', '\\\\') +
                               "\r").encode('latin1'))
        list_ofiles.close()
        _call([CXX_COMPILER, "-shared", "@%s" % list_ofiles.name,
               "-fopenmp" if OPENMP_SUPPORT else "", "-fPIC", "-flto", "-o",
               so_f.name, "-O3", "-pipe"])

        # cleanup files
        for f in o_files:
            os.unlink(f.name)
        for f in files:
            os.unlink(f.name)
        os.unlink(list_ofiles.name)
    else:
        _call([CXX_COMPILER, "-shared"] +
              [f.name for f in o_files] +
              ["-fPIC", "-flto", "-fopenmp" if OPENMP_SUPPORT else "", "-o",
               so_f.name, "-O3", "-pipe"])

    return so_f
