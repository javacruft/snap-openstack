package sunbeam

import (
	"context"
	"database/sql"
	"fmt"

	"github.com/canonical/microcluster/state"

	"github.com/canonical/snap-openstack/sunbeam-microcluster/api/types"
	"github.com/canonical/snap-openstack/sunbeam-microcluster/database"
)

// ListJujuUsers returns the jujuusers from the database
func ListJujuUsers(s *state.State) (types.JujuUsers, error) {
	users := types.JujuUsers{}

	// Get the juju users from the database.
	err := s.Database.Transaction(s.Context, func(ctx context.Context, tx *sql.Tx) error {
		records, err := database.GetJujuUsers(ctx, tx)
		if err != nil {
			return fmt.Errorf("Failed to fetch juju user: %w", err)
		}

		for _, user := range records {
			users = append(users, types.JujuUser{
				Username: user.Username,
				Token:    user.Token,
			})
		}

		return nil
	})
	if err != nil {
		return nil, err
	}

	return users, nil
}

// GetJujuUser returns a JujuUser with the given name
func GetJujuUser(s *state.State, name string) (types.JujuUser, error) {
	jujuUser := types.JujuUser{}
	err := s.Database.Transaction(s.Context, func(ctx context.Context, tx *sql.Tx) error {
		record, err := database.GetJujuUser(ctx, tx, name)
		if err != nil {
			return err
		}

		jujuUser.Username = record.Username
		jujuUser.Token = record.Token

		return nil
	})

	return jujuUser, err
}

// AddJujuUser adds a Jujuuser to the database
func AddJujuUser(s *state.State, name string, token string) error {
	// Add juju user to the database.
	err := s.Database.Transaction(s.Context, func(ctx context.Context, tx *sql.Tx) error {
		_, err := database.CreateJujuUser(ctx, tx, database.JujuUser{Username: name, Token: token})
		if err != nil {
			return fmt.Errorf("Failed to record juju user: %w", err)
		}

		return nil
	})
	if err != nil {
		return err
	}

	return nil
}

// DeleteJujuUser deletes the juju user record from the database
func DeleteJujuUser(s *state.State, name string) error {
	// Delete juju user from the database.
	err := s.Database.Transaction(s.Context, func(ctx context.Context, tx *sql.Tx) error {
		err := database.DeleteJujuUser(ctx, tx, name)
		if err != nil {
			return fmt.Errorf("Failed to delete juju user: %w", err)
		}

		return nil
	})
	if err != nil {
		return err
	}

	return nil
}
