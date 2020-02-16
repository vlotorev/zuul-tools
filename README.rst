=================
Tools for Zuul CI
=================

``submodule.py`` is a tool to replace super project submodules with corresponding `zuul.projects`__.

__ https://zuul-ci.org/docs/zuul/reference/jobs.html#var-zuul.projects

Usage
=====

Zuul job that must be run for super project should have:

* super project as required project,
* *super_project*  dictionary with `zuul.project`__:

__ https://zuul-ci.org/docs/zuul/reference/jobs.html#var-zuul.project

.. code-block:: yaml

  - job:
    ...
    required-projects:
      - <super-project>
    vars:
      super_project: "{{ zuul.projects['<super-project'] }}"

Job should run playbook that restores remote for super project, so that:

* ``.gitmodules`` entries can be matched with canonical names of ``zuul.projects``.
* submodules that are missing in ``zuul.projects`` can be cloned from remote.

After that job runs tasks that dump *zuul* variable into ``zuul.json`` and run ``submodule.py``:

.. code-block:: yaml

  tasks:
    - name: Restore remote for projects  # noqa 303
      # setup remote as zuul intentionally removes remote
      loop: "{{ zuul.projects | dict2items }}"
      command: git -C {{ item.value.src_dir }} remote set-url origin \
         ssh://{{ item.value.canonical_hostname }}:29418/{{ item.value.name }}

    - name: Dump zuul.json variable
      copy: content={{ zuul | to_nice_json }} dest=zuul.json

    - name: Prepare submodules
      command: submodule.py zuul.json {{ super_project.canonical_name }} --verbose --recursive

