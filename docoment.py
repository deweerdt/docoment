#!/usr/bin/env python2
""" Usage: call with <filename> <typename>
"""

import os
import re
import sys
import json
import shlex
import jinja2
import fnmatch
import argparse
import subprocess
import clang.cindex
import ConfigParser


def comment_to_dict(location, comment):
    brief = []
    result = {}

    def _add_param(name, value):
        if 'params' not in result:
            result['params'] = {}
        elif name in result['params']:
            print("Warning: In %s, param %s already documented." % (location, name))
        result['params'][name] = value

    for full_line in comment.split('\n'):

        line = full_line.lstrip('/*< ').rstrip('*/ ')
        if line:
            if line.startswith('@param'):
                line = line[6:].lstrip()
                try:
                    name, desc = line.split(None, 1)
                    _add_param(name, desc.strip())
                except ValueError:
                    # This should have been caught by -Wdocumentation, let's silently pass
                    pass
            elif line.startswith('@'):
                try:
                    key, value = line[1:].split(None, 1)
                except ValueError:
                    print("Warning: %s" % full_line)
                    print("Warning: ^~~ Could not extract value from: %s" % line)
                    continue
                if key in result:
                    print("Warning: %s" % full_line)
                    print("Warning: ^~~ In %s, %s already documented." % (location, key))
                result[key] = value.lstrip()
            else:
                brief.append(line)
    if brief:
        result['brief'] = '\n'.join(brief)
    return result

def get_line_in_file(path, number):
    with open(path) as f:
        i = 0
        for line in f.readlines():
            i += 1
            if i == number:
                return line.strip()

class FileMatch(object):

    def __init__(self, includes, excludes=None):
        self.inc_filters = [re.compile(fnmatch.translate(x)) for x in includes]
        self.exc_filters = [re.compile(fnmatch.translate(x)) for x in excludes] if excludes else []

    def exclude(self, s):
        for curfilter in self.exc_filters:
            if curfilter.match(s):
                return True
        return False

    def match(self, s):
        for curfilter in self.inc_filters:
            if curfilter.match(s) and not self.exclude(s):
                return True
        return False

    def filter(self, strings):
        for s in strings:
            if self.match(s) and not self.exclude(s):
                yield s

class Docoment(object):

    def __init__(self, config_file="docofile"):
        self.files = {}
        self.definitions = {}
        self.decl_types = {
            clang.cindex.CursorKind.TYPE_ALIAS_DECL: None,
            clang.cindex.CursorKind.MACRO_DEFINITION: None,
            clang.cindex.CursorKind.TYPEDEF_DECL: None,
            clang.cindex.CursorKind.ENUM_DECL: None,
            clang.cindex.CursorKind.UNION_DECL: None,
            clang.cindex.CursorKind.FUNCTION_DECL: None,
            clang.cindex.CursorKind.STRUCT_DECL: None,
        }
        self.index = clang.cindex.Index.create()

        config = ConfigParser.ConfigParser()
        config.read(config_file)
        self.project = config.get('project', 'name')
        self.paths = shlex.split(config.get('project', 'path'))
        inc_files = shlex.split(config.get('project', 'files'))
        exc_files = shlex.split(config.get('project', 'exclude')) if config.has_option('project', 'exclude') else None
        self.filematch = FileMatch(inc_files, exc_files)
        self.output_json = config.getboolean('output', 'json') if config.has_option('output', 'json') else True
        self.output_html = config.getboolean('output', 'html') if config.has_option('output', 'html') else True
        self.templates = config.get('html', 'templates') if config.has_option('html', 'templates') else './templates'
        self.extra_args = ['-Wdocumentation']
        self.extra_args.extend(self._get_default_includes())
        if config.has_option('project', 'extra_args'):
            self.extra_args.extend(shlex.split(config.get('project', 'extra_args')))
        if os.environ.get('CFLAGS'):
            self.extra_args.extend(shlex.split(os.environ.get('CFLAGS')))
        self._register_hooks()

    def _get_default_includes(self):
        regex = re.compile(ur'(?:\#include \<...\> search starts here\:)(?P<list>.*?)(?:End of search list)', re.DOTALL)
        process = subprocess.Popen(['clang', '-v', '-E', '-x', 'c', '-'], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process_out, process_err = process.communicate('')
        output = process_out + process_err
        includes = []
        for p in re.search(regex, output).group('list').split('\n'):
            p = p.strip()
            if len(p) > 0 and p.find('(framework directory)') < 0:
                includes.append('-isystem')
                includes.append(p)
        return includes

    def is_included(self, location):
        if not location.file:
            return False
        for path in self.paths:
            if path in location.file.name and self.filematch.match(location.file.name):
                return True
        return False

    def record_file(self, node):
        if node.kind == clang.cindex.CursorKind.TRANSLATION_UNIT:
            if node.spelling not in self.files:
                self.files[node.spelling] = {'definitions': {}}
            token = node.get_tokens().next()
            if token.spelling.startswith('/*'):
                self.files[node.spelling]['comment'] = comment_to_dict(node.location, token.spelling)
        elif node.location.file.name not in self.files:
            self.files[node.location.file.name] = {'definitions': {}}
            tu = self.index.parse(node.location.file.name)
            tokens = [x for x in tu.get_tokens(extent=tu.get_extent(node.location.file.name, (0, 0)))]
            if tokens and tokens[0].kind == clang.cindex.TokenKind.COMMENT:
                self.files[node.location.file.name]['comment'] = comment_to_dict(node.location, tokens[0].spelling)

    def add_definition_to_file(self, node):
        if node.location.file.name not in self.files:
            self.record_file(node)
        if node.kind.name not in self.files[node.location.file.name]['definitions']:
            self.files[node.location.file.name]['definitions'][node.kind.name] = [node.get_usr()]
        else:
            self.files[node.location.file.name]['definitions'][node.kind.name].append(node.get_usr())

    def record_definition(self, node):
        usr = node.get_usr()
        if usr and node.kind in self.decl_types:
            if usr not in self.definitions:
                self.add_definition_to_file(node)
                self.definitions[usr] = {
                    'kind': node.kind.name,
                    'spelling': node.spelling,
                    'location': {
                        'file': node.location.file.name,
                        'line': node.location.line,
                        'column': node.location.column
                    }
                }
                if node.raw_comment:
                    self.definitions[usr]['comment'] = comment_to_dict(node.location, node.raw_comment)
                func = self.decl_types[node.kind]
                if func:
                    info = func(node)
                    if info:
                        self.definitions[usr].update(info)

    def _register_hooks(self):
        def _type_id(ctype):
            decl = ctype.get_declaration()
            if decl.kind == clang.cindex.CursorKind.NO_DECL_FOUND:
                return None
            if not self.is_included(decl.location):
                return None
            return decl.get_usr()

        def _type_to_dict(ctype):
            spelling = ctype.spelling
            suffix = ''
            while ctype.kind == clang.cindex.TypeKind.POINTER:
                suffix += '*'
                ctype = ctype.get_pointee()
            return {'type': _type_id(ctype), 'type_spelling': spelling}

        def _func_to_dict(node):
            params = []
            for param in node.get_arguments():
                p = _type_to_dict(param.type)
                p['spelling'] = param.spelling
                params.append(p)
            return {'params': params, 'result': _type_to_dict(node.result_type)}
        self.decl_types[clang.cindex.CursorKind.FUNCTION_DECL] = _func_to_dict

        def _struct_to_dict(node):
            fields = []
            for field in node.type.get_fields():
                p = _type_to_dict(field.type)
                p['spelling'] = field.spelling
                if field.raw_comment:
                    p['comment'] = comment_to_dict(field.location, field.raw_comment)
                fields.append(p)
            return {'fields': fields}
        self.decl_types[clang.cindex.CursorKind.STRUCT_DECL] = _struct_to_dict

        def _enum_to_dict(node):
            fields = []
            for field in node.get_children():
                if field.kind == clang.cindex.CursorKind.ENUM_CONSTANT_DECL:
                    p = {'spelling': field.spelling, 'value': field.enum_value}
                    if field.raw_comment:
                        p['comment'] = comment_to_dict(field.location, field.raw_comment)
                    fields.append(p)
            return {'fields': fields}
        self.decl_types[clang.cindex.CursorKind.ENUM_DECL] = _enum_to_dict

        def _typedef_to_dict(node):
            return {'canonical': _type_to_dict(node.type.get_canonical())}
        self.decl_types[clang.cindex.CursorKind.TYPEDEF_DECL] = _typedef_to_dict

    def _parse_file(self, path):
        if self.filematch.exclude(path) or path.endswith('.h'):
            return
        print("Parsing %s" % path)
        tu = self.index.parse(path, args=self.extra_args,
                              options=(clang.cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD & clang.cindex.TranslationUnit.PARSE_SKIP_FUNCTION_BODIES))
        for diagnostic in tu.diagnostics:
            if diagnostic.severity > clang.cindex.Diagnostic.Warning:
                print(diagnostic)
                sys.exit(-1)
            elif diagnostic.option == '-Wdocumentation':
                print('Warning: %s' % get_line_in_file(path, diagnostic.location.line))
                print('Warning:%s^~~ %s' % ((' ' * diagnostic.location.column), diagnostic.spelling))
        nodes = [tu.cursor]
        while len(nodes):
            node = nodes.pop()
            if self.is_included(node.location):
                if node.is_definition() or node.kind == clang.cindex.CursorKind.MACRO_DEFINITION:
                    self.record_definition(node)
                else:
                    nodes.extend([c for c in node.get_children()])
            elif node.kind == clang.cindex.CursorKind.TRANSLATION_UNIT:
                self.record_file(node)
                nodes.extend([c for c in node.get_children()])

    def generate_json(self, path):
        with open(path, "w") as jf:
            json.dump({'files': self.files, 'definitions': self.definitions}, jf, indent=True, sort_keys=True)

    def generate_html(self, path):
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.templates)
        )
        template = env.get_template('func.tpl')
        with open(path, 'w') as html:
            for usr in self.definitions.keys():
                e = self.definitions[usr]
                if e['kind'] == clang.cindex.CursorKind.FUNCTION_DECL.name:
                    html.write(template.render(func=e) + '\n')

    def run(self):
        for path in self.paths:
            if os.path.isdir(path):
                for root, dirs, files in os.walk(path):
                    for curfile in self.filematch.filter(files):
                        self._parse_file(os.path.join(root, curfile))
            elif self.filematch.match(path):
                self._parse_file(path)
        if self.output_json:
            self.generate_json('result.json')
        if self.output_html:
            self.generate_html('index.html')

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", help="Path to the docofile", default="docofile")
    args = parser.parse_args()

    doc = Docoment(config_file=args.config)
    doc.run()
