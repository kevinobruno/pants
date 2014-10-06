# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (nested_scopes, generators, division, absolute_import, with_statement,
                        print_function, unicode_literals)

import os
from pkg_resources import resource_string
import pystache
import re

from pants.backend.core.tasks.console_task import ConsoleTask
from pants.backend.core.tasks.reflect import assemble_buildsyms

class TargetsHelp(ConsoleTask):
  """Show online help for symbols usable in BUILD files (java_library, etc)."""

  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    super(TargetsHelp, cls).setup_parser(option_group, args, mkflag)
    option_group.add_option(mkflag("details"), dest="goal_targets_details", default=None,
                            help='Display details about the specific target type or BUILD symbol.')

  def __init__(self, *args, **kwargs):
    super(TargetsHelp, self).__init__(*args, **kwargs)
    self._templates_dir = os.path.join('templates', 'targets_help')

  def list_all(self):
    d = assemble_buildsyms(build_file_parser=self.context.build_file_parser)
    max_sym_len = max(len(sym) for sym in d.keys())
    console = []
    blurb_template = resource_string(__name__,
                                     os.path.join(self._templates_dir,
                                                  'cli_list_blurb.mustache'))
    for sym, data in sorted(d.items(), key=lambda(k, v): k.lower()):
      blurb = pystache.render(blurb_template, data)
      summary = re.sub('\s+', ' ', blurb).strip()
      if len(summary) > 50:
        summary = summary[:47].strip() + '...'
      console.append('{0}: {1}'.format(sym.rjust(max_sym_len), summary))
    return console

  def details(self, sym):
    '''Show details of one symbol.

    :param sym: string like 'java_library' or 'artifact'.'''
    d = assemble_buildsyms(build_file_parser=self.context.build_file_parser)
    if not sym in d:
      return ['\nNo such symbol: {0}\n'.format(sym)]
    template = resource_string(__name__, os.path.join(self._templates_dir,
                                                      'cli_details.mustache'))
    spacey_render = pystache.render(template, d[sym]['defn'])
    compact_render = re.sub('\n\n+', '\n\n', spacey_render)
    return compact_render.splitlines()

  def console_output(self, targets):
    if self.context.options.goal_targets_details:
      return self.details(self.context.options.goal_targets_details)
    else:
      return self.list_all()
