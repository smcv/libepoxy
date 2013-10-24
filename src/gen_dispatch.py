#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright © 2013 Intel Corporation
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice (including the next
# paragraph) shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import sys
import argparse
import xml.etree.ElementTree as ET
import re
import os

class GLFunction(object):
    def __init__(self, ret_type, name):
        self.name = name
        self.ptr_type = 'PFN' + name.upper()
        self.ret_type = ret_type
        self.providers = []
        self.args = []

        # This is the string of C code for passing through the
        # arguments to the function.
        self.args_list = ''

        # This is the string of C code for declaring the arguments
        # list.
        self.args_decl = 'void'

        # If present, this is the string name of the function that
        # this is an alias of.  This initially comes from the
        # registry, and may get updated if it turns out our alias is
        # itself an alias (for example glFramebufferTextureEXT ->
        # glFramebufferTextureARB -> glFramebufferTexture)
        self.alias_name = None

        # After alias resolution, this is the function that this is an
        # alias of.
        self.alias_func = None

        # For the root of an alias tree, this lists the functions that
        # are marked as aliases of it, so that it can write a resolver
        # for all of them.
        self.alias_exts = []

    def add_arg(self, type, name):
        self.args.append((type, name))
        if self.args_decl == 'void':
            self.args_list = name
            self.args_decl = type + ' ' + name
        else:
            self.args_list += ', ' + name
            self.args_decl += ', ' + type + ' ' + name

    def add_provider(self, condition, loader, human_name):
        self.providers.append((condition, loader, human_name))

    def add_alias(self, ext):
        # We don't support transitivity of aliases.
        assert not ext.alias_exts
        assert self.alias_func is None

        self.alias_exts.append(ext)
        ext.alias_func = self

class Generator(object):
    def __init__(self):
        self.enums = {}
        self.functions = {}
        self.max_enum_name_len = 1
        self.copyright_comment = None
        self.typedefs = ''
        self.out_file = None

        self.dlsym_loader = 'epoxy_dlsym("{0}")'
        self.gpa_loader = 'epoxy_get_proc_address("{0}")'

    def all_text_until_element_name(self, element, element_name):
        text = ''

        if element.text is not None:
            text += element.text

        for child in element:
            if child.tag == element_name:
                break
            if child.text:
                text += child.text
            if child.tail:
                text += child.tail
        return text

    def out(self, text):
        self.out_file.write(text)

    def outln(self, text):
        self.out_file.write(text + '\n')

    def parse_typedefs(self, reg):
        for t in reg.findall('types/type'):
            if 'name' in t.attrib and t.attrib['name'] not in {'GLhandleARB'}:
                continue

            if t.text is not None:
                self.typedefs += t.text

            for child in t:
                if child.text:
                    self.typedefs += child.text
                if child.tail:
                    self.typedefs += child.tail
            self.typedefs += '\n'

    def parse_enums(self, reg):
        for enum in reg.findall('enums/enum'):
            name = enum.get('name')
            self.max_enum_name_len = max(self.max_enum_name_len, len(name))
            self.enums[name] = enum.get('value')

    def get_function_return_type(self, proto):
        # Everything up to the start of the name element is the return type.
        return self.all_text_until_element_name(proto, 'name').strip()

    def parse_function_definitions(self, reg):
        for command in reg.findall('commands/command'):
            proto = command.find('proto')
            name = proto.find('name').text
            ret_type = self.get_function_return_type(proto)

            func = GLFunction(ret_type, name)

            for arg in command.findall('param'):
                func.add_arg(self.all_text_until_element_name(arg, 'name').strip(),
                             arg.find('name').text)

            alias = command.find('alias')
            if alias is not None:
                # XXX print('{0} -> {1}'.format(func.name, alias.get('name')))
                # Note that some alias references appear before the
                # target command is defined (glAttachObjectARB() ->
                # glAttachShader(), for example).
                func.alias_name = alias.get('name')

            self.functions[name] = func

    def drop_weird_glx_functions(self):
        # Drop a few ancient SGIX GLX extensions that use types not defined
        # anywhere in Xlib.  In glxext.h, they're protected by #ifdefs for the
        # headers that defined them.
        weird_functions = [name for name, func in self.functions.items()
                           if 'VLServer' in func.args_decl
                           or 'DMparams' in func.args_decl]

        for name in weird_functions:
            del self.functions[name]

    def resolve_aliases(self):
        for func in self.functions.values():
            # Find the root of the alias tree, and add ourselves to it.
            if func.alias_name:
                alias_func = func
                while alias_func.alias_name:
                    alias_func = self.functions[alias_func.alias_name]
                func.alias_name = alias_func.name
                func.alias_func = alias_func
                alias_func.alias_exts.append(func)

    def process_require_statements(self, feature, condition, loader, human_name):
        for command in feature.findall('require/command'):
            name = command.get('name')
            func = self.functions[name]
            func.add_provider(condition, loader.format(name), human_name)

    def parse_function_providers(self, reg):
        for feature in reg.findall('feature'):
            api = feature.get('api') # string gl, gles1, gles2, glx
            m = re.match('([0-9])\.([0-9])', feature.get('number'))
            version = int(m.group(1)) * 10 + int(m.group(2))

            if api == 'gl':
                human_name = 'Desktop OpenGL {0}'.format(feature.get('number'))
                condition = 'epoxy_is_desktop_gl()'

                # Everything in GL 1.2 is guaranteed to be present as
                # public symbols in the Linux libGL ABI.  Everything
                # else is supposed to not be present, so you have to
                # glXGetProcAddress() it.
                if version <= 12:
                    loader = self.dlsym_loader
                else:
                    loader = self.gpa_loader
                    condition += ' && epoxy_gl_version() >= {0}'.format(version)
            elif api == 'gles2':
                human_name = 'OpenGL ES {0}'.format(feature.get('number'))
                condition = '!epoxy_is_desktop_gl() && epoxy_gl_version() >= {0}'.format(version)

                if version <= 20:
                    loader = self.dlsym_loader
                else:
                    loader = self.gpa_loader
            elif api == 'gles1':
                human_name = 'OpenGL ES 1.0'
                condition = '!epoxy_is_desktop_gl() && epoxy_gl_version() == 10'

                if version <= 20:
                    loader = self.dlsym_loader
                else:
                    loader = self.gpa_loader
            elif api == 'glx':
                human_name = 'GLX {0}'.format(version)
                condition = 'epoxy_glx_version() >= {0}'.format(version)
                loader = self.dlsym_loader

                if version <= 13:
                    loader = self.dlsym_loader
                else:
                    loader = self.gpa_loader
            else:
                sys.exit('unknown API: "{0}"'.format(api))

            self.process_require_statements(feature, condition, loader, human_name)

        for extension in reg.findall('extensions/extension'):
            extname = extension.get('name')
            # 'supported' is a set of strings like gl, gles1, gles2, or glx, which are
            # separated by '|'
            apis = extension.get('supported').split('|')
            if 'glx' in apis:
                human_name = 'GLX extension \\"{0}\\"'.format(extname)
                condition = 'epoxy_has_glx_extension("{0}")'.format(extname)
                loader = 'epoxy_get_proc_address("{0}")'
                self.process_require_statements(extension, condition, loader, human_name)
            if 'gl' in apis:
                human_name = 'GL extension \\"{0}\\"'.format(extname)
                condition = 'epoxy_has_gl_extension("{0}")'.format(extname)
                loader = 'epoxy_get_proc_address("{0}")'
                self.process_require_statements(extension, condition, loader, human_name)

    def fixup_bootstrap_function(self, name):
        # We handle glGetString() and glGetIntegerv() specially, because we
        # need to use them in the process of deciding on loaders for
        # resolving, and the naive code generation would result in their
        # resolvers calling their own resolvers.
        if name not in self.functions:
            return

        func = self.functions[name]
        func.providers = []
        func.add_provider('true', self.dlsym_loader.format(func.name),
                          'always present')

    def parse(self, file):
        reg = ET.parse(file)
        if reg.find('comment') != None:
            self.copyright_comment = reg.find('comment').text
        else:
            self.copyright_comment = ''
        self.parse_typedefs(reg)
        self.parse_enums(reg)
        self.parse_function_definitions(reg)
        self.parse_function_providers(reg)

    def write_copyright_comment_body(self):
        for line in self.copyright_comment.splitlines():
            if '-----' in line:
                break
            self.outln(' * ' + line)

    def write_enums(self):
        for name, value in self.enums.items():
            self.outln('#define ' + name.ljust(self.max_enum_name_len + 3) + value + '')

    def write_function_ptr_typedefs(self):
        for func in self.functions.values():
            self.outln('typedef {0} (*{1})({2});'.format(func.ret_type, func.ptr_type,
                                                         func.args_decl))

    def write_header_header(self, file):
        self.out_file = open(file, 'w')

        self.outln('/* GL dispatch header.')
        self.outln(' * This is code-generated from the GL API XML files from Khronos.')
        self.write_copyright_comment_body()
        self.outln(' */')
        self.outln('')

        self.outln('#pragma once')

        self.outln('#include <inttypes.h>')
        self.outln('#include <stddef.h>')
        self.outln('')

    def write_header(self, file):
        self.write_header_header(file)

        if 'gl_' not in file:
            self.outln('#include "epoxy/gl_generated.h"')
        else:
            # Add some ridiculous inttypes.h redefinitions that are from
            # khrplatform.h and not included in the XML.
            self.outln('typedef int8_t khronos_int8_t;')
            self.outln('typedef int16_t khronos_int16_t;')
            self.outln('typedef int32_t khronos_int32_t;')
            self.outln('typedef int64_t khronos_int64_t;')
            self.outln('typedef uint8_t khronos_uint8_t;')
            self.outln('typedef uint16_t khronos_uint16_t;')
            self.outln('typedef uint32_t khronos_uint32_t;')
            self.outln('typedef uint64_t khronos_uint64_t;')
            self.outln('typedef float khronos_float_t;')
            self.outln('typedef intptr_t khronos_intptr_t;')
            self.outln('typedef ptrdiff_t khronos_ssize_t;')

        if 'glx_' in file:
            self.outln('#include <X11/Xlib.h>')
            self.outln('#include <X11/Xutil.h>')

        self.out(self.typedefs)
        self.outln('')
        self.write_enums()
        self.outln('')
        self.write_function_ptr_typedefs()

        for func in self.functions.values():
            self.outln('{0} epoxy_{1}({2});'.format(func.ret_type, func.name,
                                                    func.args_decl))
            self.outln('')

    def write_proto_define_header(self, file, style):
        self.write_header_header(file)

        for func in self.functions.values():
            self.outln('#define {0} epoxy_{1}{0}'.format(func.name, style))

    def write_function_ptr_resolver(self, func):
        self.outln('static {0}'.format(func.ptr_type))
        self.outln('epoxy_{0}_resolver(void)'.format(func.name))
        self.outln('{')
        if 'glX' in func.name:
            self.outln('    epoxy_glx_autoinit();')
        else:
            self.outln('    epoxy_platform_autoinit();')
        self.outln('')

        providers = []
        # Make a local list of all the providers for this alias group
        for provider in func.providers:
            providers.append(provider)
        for alias_func in func.alias_exts:
            for provider in alias_func.providers:
                providers.append(provider)

        # Check if there's any alias of this that's built into our
        # ABI, and just use that one if available.  This is important
        # for avoiding loops of
        # glXGetProcAddress(glXGetProcAddress()), or
        # glGetIntegerv(glGetIntegerv()).
        global_loader = None
        for condition, loader, human_name in providers:
            if condition == 'true':
                global_loader = loader

        if global_loader:
            self.outln('   return {0};'.format(loader))
        else:
            for condition, loader, human_name in providers:
                self.outln('    if ({0})'.format(condition))
                self.outln('        return {0};'.format(loader))
            self.outln('')

        # If the function isn't provided by any known extension, print
        # something useful for the poor application developer before
        # aborting.  (In non-epoxy GL usage, the app developer would
        # call into some blank stub function and segfault).
        self.outln('    printf("No provider of \\"{0}()\\" found.  Requires one of:\\n");'.format(func.name))
        if len(func.providers) == 0:
            self.outln('    printf("    unknown\\n");')
        else:
            for provider in func.providers:
                self.outln('    printf("    {0}\\n");'.format(provider[2]))

        self.outln('    abort();')

        self.outln('}')
        self.outln('')

    def write_dispatch_table_stub(self, func):
        # Use the same resolver for all the aliases of a particular
        # function.
        alias_name = func.name
        if func.alias_name:
            alias_name = func.alias_name

        dispatch_table_entry = 'dispatch_table->p{0}'.format(alias_name)

        self.outln('PUBLIC {0}'.format(func.ret_type))
        self.outln('epoxy_{0}({1})'.format(func.name, func.args_decl))
        self.outln('{')
        self.outln('    if (!{0})'.format(dispatch_table_entry))
        self.outln('        {0} = epoxy_{1}_resolver();'.format(dispatch_table_entry,
                                                                alias_name))
        self.outln('')
        if func.ret_type == 'void':
            self.outln('    {0}({1});'.format(dispatch_table_entry, func.args_list))
        else:
            self.outln('    return {0}({1});'.format(dispatch_table_entry, func.args_list))
        self.outln('}')
        self.outln('')

    def write_source(self, file):
        self.out_file = open(file, 'w')

        self.outln('/* GL dispatch code.')
        self.outln(' * This is code-generated from the GL API XML files from Khronos.')
        self.write_copyright_comment_body()
        self.outln(' */')
        self.outln('')
        self.outln('#include <stdlib.h>')
        self.outln('#include <stdio.h>')
        self.outln('')
        self.outln('#include "dispatch_common.h"')
        if 'glx_' in file:
            self.outln('#include "epoxy/glx_generated.h"')
        else:
            self.outln('#include "epoxy/gl_generated.h"')
        self.outln('')

        self.outln('struct dispatch_table {')
        for func in self.functions.values():
            # Aliases don't get their own slot, since they use a shared resolver.
            if not func.alias_name:
                self.outln('    {0} p{1};'.format(func.ptr_type, func.name))
        self.outln('};')
        self.outln('')

        self.outln('/* XXX: Make this thread-local and swapped on makecurrent. */')
        self.outln('static struct dispatch_table local_dispatch_table;')
        self.outln('static struct dispatch_table *dispatch_table = &local_dispatch_table;')
        self.outln('')

        for func in self.functions.values():
            if not func.alias_func:
                self.write_function_ptr_resolver(func)

        for func in self.functions.values():
            self.write_dispatch_table_stub(func)

argparser = argparse.ArgumentParser(description='Generate GL dispatch wrappers.')
argparser.add_argument('files', metavar='file.xml', nargs='+', help='GL API XML files to be parsed')
argparser.add_argument('--dir', metavar='dir', required=True, help='Destination directory')
args = argparser.parse_args()

srcdir = args.dir + '/src/'
incdir = args.dir + '/include/epoxy/'

for file in args.files:
    name = os.path.basename(file).split('.xml')[0]
    generator = Generator()
    generator.parse(file)
    generator.drop_weird_glx_functions()
    generator.resolve_aliases()
    generator.fixup_bootstrap_function('glGetString')
    generator.fixup_bootstrap_function('glGetIntegerv')

    # While this is technically exposed as a GLX extension, it's
    # required to be present as a public symbol by the Linux OpenGL
    # ABI.
    generator.fixup_bootstrap_function('glXGetProcAddress')

    generator.write_header(incdir + name + '_generated.h')
    generator.write_proto_define_header(incdir + name + '_generated_vtable_defines.h', '')
    generator.write_source(srcdir + name + '_generated_dispatch.c')