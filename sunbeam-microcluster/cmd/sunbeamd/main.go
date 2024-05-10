// Package sunbeamd provides the cluster daemon.
package main

import (
	"context"
	"math/rand"
	"os"
	"time"

	"github.com/canonical/lxd/shared/logger"
	"github.com/canonical/microcluster/config"
	"github.com/canonical/microcluster/microcluster"
	"github.com/canonical/microcluster/state"
	"github.com/spf13/cobra"

	"github.com/canonical/snap-openstack/sunbeam-microcluster/api"
	"github.com/canonical/snap-openstack/sunbeam-microcluster/database"
	"github.com/canonical/snap-openstack/sunbeam-microcluster/version"
)

// Debug indicates whether to log debug messages or not.
var Debug bool

// Verbose indicates verbosity.
var Verbose bool

type cmdGlobal struct {
	cmd *cobra.Command //nolint:structcheck,unused // FIXME: Remove the nolint flag when this is in use.

	flagHelp    bool
	flagVersion bool

	flagLogDebug   bool
	flagLogVerbose bool
}

func (c *cmdGlobal) Run(_ *cobra.Command, _ []string) error {
	Debug = c.flagLogDebug
	Verbose = c.flagLogVerbose

	return logger.InitLogger("", "", c.flagLogVerbose, c.flagLogDebug, nil)
}

type cmdDaemon struct {
	global *cmdGlobal

	flagStateDir    string
	flagSocketGroup string
}

func (c *cmdDaemon) Command() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "sunbeamd",
		Short:   "Cluster daemon for sunbeam",
		Version: version.Version,
	}

	cmd.RunE = c.Run

	return cmd
}

func (c *cmdDaemon) Run(_ *cobra.Command, _ []string) error {
	m, err := microcluster.App(microcluster.Args{StateDir: c.flagStateDir, SocketGroup: c.flagSocketGroup, Verbose: c.global.flagLogVerbose, Debug: c.global.flagLogDebug})
	if err != nil {
		return err
	}

	// Placeholder for post-action hooks that can be run by MicroCluster.
	h := &config.Hooks{
		// PreBootstrap is before after the daemon is initialized and bootstrapped.
		PreBootstrap: func(_ *state.State, _ map[string]string) error {
			logger.Info("This is a hook that runs before the daemon is initialized and bootstrapped")

			return nil
		},

		// PostBootstrap is run after the daemon is initialized and bootstrapped.
		PostBootstrap: func(_ *state.State, _ map[string]string) error {
			logger.Info("This is a hook that runs after the daemon is initialized and bootstrapped")

			return nil
		},

		// OnStart is run after the daemon is started.
		OnStart: func(_ *state.State) error {
			logger.Info("This is a hook that runs after the daemon first starts")

			return nil
		},

		// PostJoin is run after the daemon is initialized and joins a cluster.
		PostJoin: func(_ *state.State, _ map[string]string) error {
			logger.Info("This is a hook that runs after the daemon is initialized and joins an existing cluster, after OnNewMember runs on all peers")

			return nil
		},

		// PreJoin is run after the daemon is initialized and joins a cluster.
		PreJoin: func(_ *state.State, _ map[string]string) error {
			logger.Info("This is a hook that runs after the daemon is initialized and joins an existing cluster, before OnNewMember runs on all peers")

			return nil
		},

		// PostRemove is run after the daemon is removed from a cluster.
		PostRemove: func(s *state.State, _ bool) error {
			logger.Infof("This is a hook that is run on peer %q after a cluster member is removed", s.Name())

			return nil
		},

		// PreRemove is run before the daemon is removed from the cluster.
		PreRemove: func(s *state.State, _ bool) error {
			logger.Infof("This is a hook that is run on peer %q just before it is removed", s.Name())

			return nil
		},

		// OnHeartbeat is run after a successful heartbeat round.
		OnHeartbeat: func(_ *state.State) error {
			logger.Info("This is a hook that is run on the dqlite leader after a successful heartbeat")

			return nil
		},

		// OnNewMember is run after a new member has joined.
		OnNewMember: func(s *state.State) error {
			logger.Infof("This is a hook that is run on peer %q when a new cluster member has joined", s.Name())

			return nil
		},
	}

	return m.Start(context.Background(), api.Endpoints, database.SchemaExtensions, h)
}

func init() {
	rand.New(rand.NewSource(time.Now().UnixNano()))
}

func main() {
	daemonCmd := cmdDaemon{global: &cmdGlobal{}}
	app := daemonCmd.Command()
	app.SilenceUsage = true
	app.CompletionOptions = cobra.CompletionOptions{DisableDefaultCmd: true}

	app.PersistentFlags().BoolVarP(&daemonCmd.global.flagHelp, "help", "h", false, "Print help")
	app.PersistentFlags().BoolVar(&daemonCmd.global.flagVersion, "version", false, "Print version number")
	app.PersistentFlags().BoolVarP(&daemonCmd.global.flagLogDebug, "debug", "d", false, "Show all debug messages")
	app.PersistentFlags().BoolVarP(&daemonCmd.global.flagLogVerbose, "verbose", "v", false, "Show all information messages")

	app.PersistentFlags().StringVar(&daemonCmd.flagStateDir, "state-dir", "", "Path to store state information"+"``")
	app.PersistentFlags().StringVar(&daemonCmd.flagSocketGroup, "socket-group", "", "Group to set socket's group ownership to")

	app.SetVersionTemplate("{{.Version}}\n")

	err := app.Execute()
	if err != nil {
		os.Exit(1)
	}
}
