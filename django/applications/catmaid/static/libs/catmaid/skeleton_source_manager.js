/* -*- mode: espresso; espresso-indent-level: 2; indent-tabs-mode: nil -*- */
/* vim: set softtabstop=2 shiftwidth=2 tabstop=2 expandtab: */

(function(CATMAID) {

  // A prototype for a manager of existing skeleton sources
  var SkeletonSourceManager = function() {
    this.sources = {};

    // Register with neuron manager to get updates about deleted neurons
    CATMAID.neuronController.on(CATMAID.neuronController.EVENT_SKELETON_DELETED,
      function(skeletonID) { this.removeSkeletons([skeletonID]); }, this);
  };

  SkeletonSourceManager.prototype = {};

  SkeletonSourceManager.prototype.add = function(source) {
    this.sources[source.getName()] = source;
  };

  SkeletonSourceManager.prototype.remove = function(source) {
    delete this.sources[source.getName()];
    this.updateGUI();
    Object.keys(this.sources).forEach(function(name) {
      var s = this.sources[name];
      if (s.linkTarget === source) delete s.linkTarget;
    }, this);
  };

  SkeletonSourceManager.prototype.createOptions = function() {
    return Object.keys(this.sources).sort().map(function(name) {
      return new Option(name, name);
    }, this);
  };

  /** Updates all existing 'select' GUI elements listing sources.
   *  Assumes names are unique. */
  SkeletonSourceManager.prototype.updateGUI = function() {
    var options = this.createOptions.bind(this);
    var sources = this.sources;
    $("[id^='skeleton-source-select-']").each(function(index, select) {
      var ipush = this.id.indexOf('-push-');
      var name = (-1 === ipush ? this.id.substring(23) : this.id.substring(23, ipush)).replace(/-/g, ' ');
      var selectedIndex = select.selectedIndex === -1 ? 0 : select.selectedIndex;
      var selected = select.options[selectedIndex].value;
      select.options.length = select.options[0].value === 'None' ? 1 : 0; // preserve manually added initial void entry when present in push selects
      select.selectedIndex = 0;
      options().forEach(function(option, i) {
        if (option.value === name) return; // ignore self
        select.options.add(option);
        if (option.value === selected) select.selectedIndex = select.options.length -1;
      });
    });
  };

  SkeletonSourceManager.prototype.createSelectID = function(source) {
    return 'skeleton-source-select-' + source.getName().replace(/ /g, '-');
  };

  SkeletonSourceManager.prototype.createSelect = function(source) {
    var select = document.createElement('select');
    select.setAttribute('id', this.createSelectID(source));
    var name = source.getName();
    this.createOptions().forEach(function(option, i) {
      if (option.value !== name) select.options.add(option);
      if (option.value === 'Active skeleton') select.selectedIndex = i;
    });
    return select;
  };

  SkeletonSourceManager.prototype.createPushSelect = function(source, suffix) {
    var select = document.createElement('select');
    select.setAttribute('id', this.createSelectID(source) + '-push-' + suffix);
    select.options.add(new Option('None', 'None'));
    var name = source.getName();
    this.createOptions().forEach(function(option) {
      if (option.value !== name) select.options.add(option);
    });
    return select;
  };

  SkeletonSourceManager.prototype.getSelectedSource = function(ref_source) {
    return this.sources[$('#' + this.createSelectID(ref_source)).val()];
  };

  SkeletonSourceManager.prototype.getSelectedPushSource = function(ref_source, suffix) {
    return this.sources[$('#' + this.createSelectID(ref_source) + "-push-" + suffix).val()];
  };

  SkeletonSourceManager.prototype.getSource = function(name) {
    return this.sources[name];
  };

  SkeletonSourceManager.prototype.getSelectedSkeletons = function(ref_source) {
    var source = this.getSelectedSource(ref_source);
    if (!source) {
      console.log("No source found for reference source " + ref_source.getName());
      return [];
    }
    var skeletons = source.getSelectedSkeletons();
    if (0 === skeletons.length) {
      growlAlert('Info', 'No skeletons available at ' + source.getName());
    }
    return skeletons;
  };

  SkeletonSourceManager.prototype.getSelectedSkeletonModels = function(ref_source) {
    var source = this.getSelectedSource(ref_source);
    if (!source) {
      console.log("No source found for reference source " + ref_source.getName());
      return [];
    }
    var models = source.getSelectedSkeletonModels();
    if (0 === models.length) {
      growlAlert('Info', 'No skeletons selected at ' + source.getName());
    }
    return models;
  };

  /** Return the subset of models not present in source. */
  SkeletonSourceManager.prototype.findDifference = function(source, models) {
      return Object.keys(models).reduce(function(o, skid) {
        if (!source.hasSkeleton(skid)) o[skid] = models[skid];
        return o;
      }, {});
  };

  SkeletonSourceManager.prototype.highlight = function(caller, skeleton_id) {
    Object.keys(this.sources).forEach(function(name) {
      var source = this.sources[name];
      if (source === caller) return;
      source.highlight(skeleton_id);
    }, this);
  };

  SkeletonSourceManager.prototype.removeSkeletons = function(skeleton_ids) {
    Object.keys(this.sources).forEach(function(name) {
      this.sources[name].removeSkeletons(skeleton_ids);
    }, this);
  };

  SkeletonSourceManager.prototype.setVisible = function(skeleton_ids, visible) {
    Object.keys(this.sources).forEach(function(name) {
      var source = this.sources[name];
      if (typeof(source['setVisible']) === 'function') {
        source.setVisible(skeleton_ids, visible);
      }
    }, this);
  };

  // Make source manager available in CATMAID namespace
  CATMAID.SkeletonSourceManager = SkeletonSourceManager;

  // Create a default instance within the CATMAID namespace
  CATMAID.skeletonListSources = new SkeletonSourceManager();

})(CATMAID);