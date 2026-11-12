Breaking: the `ml` extra is removed; scikit-learn and lightgbm are now base dependencies, so drop `[ml]` from installs.

### Changed (classifier stack in the base install, SCAN-ML M2, 2026-11-11)

`scikit-learn==1.7.2` and `lightgbm==4.6.0` moved from the `ml` extra to the base dependencies, so
every install can run the bundled lgbm-v1 field classifier. The pins are unchanged. On macOS
LightGBM needs `brew install libomp`; without it `classifier_status()` reports the `import` cause
and nothing raises. The certified DP closure changed with the install set, so
`_CERTIFIED_STACKS` gains two rows (engine dev profile and the CLI runtime closure). Older rows
stay. NOTICE is regenerated, and CI gains a `base-install` job that runs the classifier from a
no-extras install.
